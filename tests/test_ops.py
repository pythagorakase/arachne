from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest


REPO = Path(__file__).resolve().parents[1]


def run_bash(script: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def flock_held(path: Path) -> bool:
    import fcntl
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def hold_flock(path: Path) -> subprocess.Popen[str]:
    """Hold an exclusive flock on *path* from another process until killed."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "d = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
         "fcntl.flock(d, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(60)\n", str(path)],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
    return holder


def system_ca_bundle() -> str | None:
    candidates = [
        ssl.get_default_verify_paths().cafile,
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class BackendTlsTests(unittest.TestCase):
    def test_generation_is_private_valid_and_idempotent(self) -> None:
        bundle = system_ca_bundle()
        if bundle is None:
            self.skipTest("system CA bundle is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            tls_dir = root / "tls"
            env = {
                **os.environ,
                "HOME": temporary,
                "ARACHNE_DATA_DIR": str(data),
                "ARACHNE_TLS_DIR": str(tls_dir),
                "ARACHNE_SYSTEM_CA_BUNDLE": bundle,
            }
            first = run_bash(REPO / "bin/init-backend-tls.sh", env)
            self.assertEqual(first.returncode, 0, first.stderr)
            files = [
                tls_dir / "ca-key.pem",
                tls_dir / "ca-cert.pem",
                tls_dir / "server-key.pem",
                tls_dir / "server-cert.pem",
                tls_dir / "trust-bundle.pem",
            ]
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files
            }
            self.assertEqual(tls_dir.stat().st_mode & 0o777, 0o700)
            for path in files:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            second = run_bash(REPO / "bin/init-backend-tls.sh", env)
            self.assertEqual(second.returncode, 0, second.stderr)
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files
            }
            self.assertEqual(after, before)

    def test_partial_state_fails_loud(self) -> None:
        bundle = system_ca_bundle()
        if bundle is None:
            self.skipTest("system CA bundle is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            tls_dir = Path(temporary) / "tls"
            tls_dir.mkdir(parents=True, mode=0o700)
            (tls_dir / "ca-key.pem").write_text("partial", encoding="ascii")
            os.chmod(tls_dir / "ca-key.pem", 0o600)
            result = run_bash(
                REPO / "bin/init-backend-tls.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "ARACHNE_DATA_DIR": str(Path(temporary) / "data"),
                    "ARACHNE_TLS_DIR": str(tls_dir),
                    "ARACHNE_SYSTEM_CA_BUNDLE": bundle,
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("partial, stale, or invalid", result.stderr)

    def test_system_ca_change_refreshes_only_the_derived_trust_bundle(self) -> None:
        bundle = system_ca_bundle()
        if bundle is None:
            self.skipTest("system CA bundle is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mutable_bundle = root / "system-ca.pem"
            shutil.copyfile(bundle, mutable_bundle)
            tls_dir = root / "tls"
            env = {
                **os.environ,
                "HOME": temporary,
                "ARACHNE_TLS_DIR": str(tls_dir),
                "ARACHNE_SYSTEM_CA_BUNDLE": str(mutable_bundle),
            }
            first = run_bash(REPO / "bin/init-backend-tls.sh", env)
            self.assertEqual(first.returncode, 0, first.stderr)
            identity_files = (
                tls_dir / "ca-key.pem",
                tls_dir / "ca-cert.pem",
                tls_dir / "server-key.pem",
                tls_dir / "server-cert.pem",
            )
            identity_before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in identity_files
            }
            trust_before = hashlib.sha256(
                (tls_dir / "trust-bundle.pem").read_bytes()
            ).hexdigest()

            with mutable_bundle.open("ab") as stream:
                stream.write(b"\n# simulated system CA refresh\n")
            second = run_bash(REPO / "bin/init-backend-tls.sh", env)
            self.assertEqual(second.returncode, 0, second.stderr)
            identity_after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in identity_files
            }
            trust_after = hashlib.sha256(
                (tls_dir / "trust-bundle.pem").read_bytes()
            ).hexdigest()
            self.assertEqual(identity_after, identity_before)
            self.assertNotEqual(trust_after, trust_before)
            self.assertEqual((tls_dir / "trust-bundle.pem").stat().st_mode & 0o777, 0o600)


class WakeSignalTests(unittest.TestCase):
    def test_endpoint_is_required_before_reading_or_sending_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token = Path(temporary) / "auth-token"
            token.write_text("A" * 32 + "\n", encoding="ascii")
            environment = {**os.environ, "ARACHNE_TOKEN_FILE": str(token)}
            environment.pop("ARACHNE_URL", None)
            result = run_bash(REPO / "bin/arm-wake.sh", environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ARACHNE_URL must name", result.stderr)

    def test_term_exits_and_runs_exit_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mock_bin = root / "bin"
            mock_bin.mkdir()
            fake_curl = mock_bin / "curl"
            fake_curl.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
            fake_curl.chmod(0o755)
            token = root / "auth-token"
            token.write_text("A" * 32 + "\n", encoding="ascii")
            tmp_dir = root / "tmp"
            tmp_dir.mkdir()
            process = subprocess.Popen(
                ["bash", str(REPO / "bin/arm-wake.sh")],
                env={
                    **os.environ,
                    "PATH": f"{mock_bin}{os.pathsep}{os.environ['PATH']}",
                    "TMPDIR": str(tmp_dir),
                    "ARACHNE_TOKEN_FILE": str(token),
                    "ARACHNE_CURSOR_FILE": str(root / "cursor"),
                    "ARACHNE_URL": "https://arachne.invalid",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not list(tmp_dir.glob("arachne-wake.*")) and time.monotonic() < deadline:
                time.sleep(0.02)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 143, (stdout, stderr))
            self.assertEqual(list(tmp_dir.glob("arachne-wake.*")), [])


@unittest.skipUnless(sys.platform == "darwin", "macOS launchctl integration")
class CodexClientSupportTests(unittest.TestCase):
    def fake_launchctl(self, root: Path) -> Path:
        script = root / "launchctl"
        script.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARACHNE_LAUNCHCTL_CAPTURE"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_token_export_validates_and_sets_gui_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = root / "auth-token"
            token.write_text("A" * 44 + "\n", encoding="ascii")
            token.chmod(0o600)
            capture = root / "arguments"
            result = run_bash(
                REPO / "bin/export-codex-mcp-token.sh",
                {
                    **os.environ,
                    "ARACHNE_TOKEN_FILE": str(token),
                    "ARACHNE_LAUNCHCTL": str(self.fake_launchctl(root)),
                    "ARACHNE_LAUNCHCTL_CAPTURE": str(capture),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                ["setenv", "ARACHNE_MCP_TOKEN", "A" * 44],
            )

    def test_token_export_rejects_group_readable_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = root / "auth-token"
            token.write_text("A" * 44 + "\n", encoding="ascii")
            token.chmod(0o640)
            result = run_bash(
                REPO / "bin/export-codex-mcp-token.sh",
                {
                    **os.environ,
                    "ARACHNE_TOKEN_FILE": str(token),
                    "ARACHNE_LAUNCHCTL": str(self.fake_launchctl(root)),
                    "ARACHNE_LAUNCHCTL_CAPTURE": str(root / "arguments"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("group or other access", result.stderr)

    def test_installer_links_skill_and_writes_secret_free_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            agents_dir = root / "LaunchAgents"
            result = run_bash(
                REPO / "bin/install-codex-client-support.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "CODEX_SKILLS_ROOT": str(skill_root),
                    "ARACHNE_LAUNCH_AGENTS_DIR": str(agents_dir),
                    "ARACHNE_INSTALL_NO_BOOTSTRAP": "1",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            skill = skill_root / "arachne"
            self.assertTrue(skill.is_symlink())
            self.assertEqual(skill.resolve(), REPO / "plugin/skills/arachne")
            plist = agents_dir / "com.pythagorakase.arachne.codex-env.plist"
            self.assertEqual(plist.stat().st_mode & 0o777, 0o600)
            contents = plist.read_text(encoding="utf-8")
            self.assertIn(str(REPO / "bin/export-codex-mcp-token.sh"), contents)
            self.assertNotIn("A" * 32, contents)


class MacOpsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deploy = self.root / "deployment.env"
        self.config = {"ARACHNE_PYTHON": sys.executable}
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "ARACHNE_DEPLOY_ENV": str(self.deploy),
        }

    def configure(self, **values: str) -> None:
        self.config.update(values)
        self.deploy.write_text("".join(
            f"{key}={shlex.quote(value)}\n" for key, value in self.config.items()
        ), encoding="utf-8")
        self.deploy.chmod(0o600)


class MacOpsTests(MacOpsBase):
    def test_environment_file_checks(self) -> None:
        self.configure()
        for script in ("pull-state-backup.sh", "watch-primary.sh"):
            with self.subTest(script=script):
                self.deploy.chmod(0o640)
                result = run_bash(REPO / "bin" / script, self.env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("deny group/other access", result.stderr)
                self.deploy.chmod(0o600)
                link = self.root / "linked.env"
                link.symlink_to(self.deploy)
                result = run_bash(REPO / "bin" / script,
                                  {**self.env, "ARACHNE_DEPLOY_ENV": str(link)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owner-controlled", result.stderr)
                link.unlink()

    def test_launchagent_schedules_and_paths(self) -> None:
        for kind, script in (("backup", "pull-state-backup.sh"),
                             ("watch", "watch-primary.sh")):
            template = REPO / "deploy/macos" / f"com.pythagorakase.arachne.{kind}.plist.in"
            contents = template.read_text(encoding="utf-8")
            for key, value in {"ARACHNE_ROOT": str(REPO),
                               "ARACHNE_DEPLOY_ENV": str(self.deploy),
                               "ARACHNE_RUNTIME_DIR": str(self.root)}.items():
                contents = contents.replace(f"@@{key}@@", value)
            plist = plistlib.loads(contents.encode())
            self.assertEqual(plist["ProgramArguments"], [str(REPO / "bin" / script)])
            self.assertEqual(plist["EnvironmentVariables"]["ARACHNE_DEPLOY_ENV"], str(self.deploy))
            self.assertEqual(plist["StandardOutPath"], str(self.root / f"{kind}.log"))
            self.assertEqual(plist["StandardErrorPath"], plist["StandardOutPath"])
            if kind == "backup":
                self.assertEqual(plist["StartCalendarInterval"], {"Hour": 3, "Minute": 17})
            else:
                self.assertEqual(plist["StartInterval"], 300)
                self.assertIs(plist["RunAtLoad"], True)


@unittest.skipUnless(shutil.which("rsync"), "rsync is required")
class StateBackupTests(MacOpsBase):
    def setUp(self) -> None:
        super().setUp()
        from server import RulingStore
        self.state = self.root / "source state"
        self.store = RulingStore(self.state)
        self.store.file("first", "First ruling", {})
        self.store.file("second", "Second ruling", {})
        (self.state / "auth-token").write_text("private-test-token\n", encoding="utf-8")
        (self.state / "auth-token").chmod(0o600)
        for name in ("dismissals", "shares"):
            (self.state / name).mkdir()
            (self.state / name / "record.json").write_text("{}", encoding="utf-8")
        self.pages = self.root / "source pages"
        self.pages.mkdir()
        (self.pages / "index.html").write_text("<h1>Decision</h1>", encoding="utf-8")
        self.backups = self.root / "backups"
        self.configure(ARACHNE_BACKUP_STATE_SRC=str(self.state),
                       ARACHNE_BACKUP_PAGES_SRC=str(self.pages),
                       ARACHNE_BACKUP_DIR=str(self.backups))

    def run_backup(self, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = run_bash(REPO / "bin/pull-state-backup.sh", self.env)
        self.assertEqual(result.returncode == 0, success, (result.stdout, result.stderr))
        self.assertEqual(len((result.stdout + result.stderr).splitlines()), 1)
        self.assertNotIn("private-test-token", result.stdout + result.stderr)
        self.assertFalse(list(self.backups.glob("*.partial")))
        lock = self.backups / ".backup.lock"
        if lock.exists():
            self.assertFalse(flock_held(lock))
        return result

    def snapshots(self) -> list[Path]:
        return sorted(path for path in self.backups.iterdir()
                      if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", path.name)
                      and (path / "MANIFEST.json").is_file())

    def test_snapshot_manifest_dedup_and_pruning(self) -> None:
        self.run_backup()
        first = (self.backups / "latest").resolve()
        manifest = json.loads((first / "MANIFEST.json").read_text())
        self.assertEqual(manifest["latest_sequence"], 2)
        self.assertEqual(manifest["ruling_count"], 2)
        self.assertEqual(manifest["file_count"], 8)
        self.assertEqual(manifest["sources"], {"state": str(self.state), "pages": str(self.pages)})
        self.assertTrue(manifest["created_at"].endswith("Z"))
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        self.assertEqual((first / "MANIFEST.json").stat().st_mode & 0o777, 0o600)
        for component, source in (("state", self.state), ("pages", self.pages)):
            for path in source.rglob("*"):
                if path.is_file():
                    self.assertEqual((first / component / path.relative_to(source)).read_bytes(), path.read_bytes())
        self.run_backup()
        second = (self.backups / "latest").resolve()
        self.assertNotEqual(first, second)
        for relative in ("pages/index.html", "state/auth-token", "state/shares/record.json"):
            self.assertEqual((first / relative).stat().st_ino, (second / relative).stat().st_ino)
        # Unrelated files, incomplete timestamp directories, and symlinks survive pruning.
        (self.backups / "notes").write_text("keep", encoding="utf-8")
        incomplete = self.backups / "20000101T000000Z"
        incomplete.mkdir()
        link = self.backups / "20000102T000000Z"
        link.symlink_to(second, target_is_directory=True)
        (self.pages / "index.html").write_text("<h1>Changed decision</h1>", encoding="utf-8")
        self.configure(ARACHNE_BACKUP_KEEP="2")
        self.run_backup()
        third = (self.backups / "latest").resolve()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertNotEqual((second / "pages/index.html").stat().st_ino,
                            (third / "pages/index.html").stat().st_ino)
        self.assertEqual((second / "pages/index.html").read_text(), "<h1>Decision</h1>")
        self.assertTrue(incomplete.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual((self.backups / "notes").read_text(), "keep")

    def test_bad_source_and_corrupt_store_do_not_publish(self) -> None:
        self.run_backup()
        before = self.snapshots()
        latest = os.readlink(self.backups / "latest")
        self.configure(ARACHNE_BACKUP_PAGES_SRC=str(self.root / "missing"))
        self.run_backup(success=False)
        self.assertEqual(self.snapshots(), before)
        self.assertEqual(os.readlink(self.backups / "latest"), latest)
        self.configure(ARACHNE_BACKUP_PAGES_SRC=str(self.pages))
        ruling = next((self.state / "rulings").glob("*.json"))
        ruling.write_text("corrupt private-test-token", encoding="utf-8")
        self.run_backup(success=False)
        self.assertEqual(self.snapshots(), before)
        self.assertEqual(os.readlink(self.backups / "latest"), latest)

    def test_lock_contention_is_a_clean_noop(self) -> None:
        self.backups.mkdir()
        lock = self.backups / ".backup.lock"
        holder = hold_flock(lock)
        try:
            result = run_bash(REPO / "bin/pull-state-backup.sh", self.env)
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("locked", result.stdout)
        self.assertEqual(self.snapshots(), [])

    def test_lock_of_a_killed_run_does_not_block(self) -> None:
        self.backups.mkdir()
        lock = self.backups / ".backup.lock"
        holder = hold_flock(lock)
        holder.kill()
        holder.wait()
        self.run_backup()
        self.assertEqual(len(self.snapshots()), 1)

    def test_missing_rulings_or_regressed_sequence_never_prunes_good_snapshots(self) -> None:
        self.configure(ARACHNE_BACKUP_KEEP="1")
        self.run_backup()
        good = self.snapshots()
        latest = os.readlink(self.backups / "latest")
        rulings = self.state / "rulings"
        shutil.move(rulings, self.root / "moved rulings")
        self.run_backup(success=False)
        self.assertEqual(self.snapshots(), good)
        self.assertEqual(os.readlink(self.backups / "latest"), latest)
        rulings.mkdir()
        # Keep only the first ruling: a valid store whose sequence went backward.
        shutil.copy(sorted((self.root / "moved rulings").glob("*.json"))[0], rulings)
        self.run_backup(success=False)
        self.assertEqual(self.snapshots(), good)
        self.assertEqual(os.readlink(self.backups / "latest"), latest)

    def test_dangling_token_and_regression_against_any_retained_snapshot(self) -> None:
        self.configure(ARACHNE_BACKUP_KEEP="2")
        self.run_backup()
        good = self.snapshots()
        token = self.state / "auth-token"
        token.unlink()
        token.symlink_to(self.root / "nowhere")
        self.run_backup(success=False)
        self.assertEqual(self.snapshots(), good)
        token.unlink()
        token.write_text("private-test-token\n", encoding="utf-8")
        token.chmod(0o600)
        # A future-dated older snapshot sorts last; the newer, higher-sequence
        # snapshot must still set the baseline.
        future = self.backups / "29991231T235959Z"
        good[0].rename(future)
        self.store.file("third", "Third ruling", {})
        self.run_backup()
        [current] = [path for path in self.snapshots() if path != future]
        third = sorted((self.state / "rulings").glob("*.json"))[-1]
        third.unlink()
        self.run_backup(success=False)
        self.assertTrue(current.exists())

    def test_clock_step_backward_keeps_the_new_snapshot(self) -> None:
        self.configure(ARACHNE_BACKUP_KEEP="1")
        self.run_backup()
        future = self.backups / "29991231T235959Z"
        self.snapshots()[0].rename(future)
        os.replace(self.backups / "latest", self.backups / "latest.old")
        (self.backups / "latest").symlink_to(future.name)
        (self.backups / "latest.old").unlink()
        self.run_backup()
        [kept] = self.snapshots()
        self.assertNotEqual(kept, future)
        self.assertEqual((self.backups / "latest").resolve(), kept.resolve())

    def test_invalid_retention_does_not_prune(self) -> None:
        self.run_backup()
        before = self.snapshots()
        for keep in ("0", "-1", "two"):
            self.configure(ARACHNE_BACKUP_KEEP=keep)
            self.run_backup(success=False)
            self.assertEqual(self.snapshots(), before)

    def test_regular_latest_is_not_overwritten(self) -> None:
        self.backups.mkdir()
        (self.backups / "latest").write_text("unrelated", encoding="utf-8")
        self.run_backup(success=False)
        self.assertEqual((self.backups / "latest").read_text(), "unrelated")
        self.assertEqual(self.snapshots(), [])


@unittest.skipUnless(shutil.which("openssl") and shutil.which("curl"), "openssl and curl are required")
class PrimaryWatchTests(MacOpsBase):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = self.root / "runtime"
        tls = self.root / "tls"
        result = run_bash(REPO / "bin/init-backend-tls.sh", {
            **self.env, "ARACHNE_TLS_DIR": str(tls),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.responses = {"/primary": (200, b'{"ok":true}'),
                          "/control": (200, b'{"ok":true}')}
        responses = self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = responses[self.path]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls / "server-cert.pem", tls / "server-key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.base = f"https://127.0.0.1:{server.server_port}"
        self.calls = self.root / "alerts.jsonl"
        command = self.root / "record alert"
        command.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(self.calls)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8")
        command.chmod(0o700)
        self.configure(ARACHNE_PRIMARY_HEALTH_URL=self.base + "/primary",
                       ARACHNE_RUNTIME_DIR=str(self.runtime),
                       ARACHNE_ALERT_COMMAND=str(command),
                       ARACHNE_PUBLIC_URL='https://standby.invalid/"quoted"',
                       CURL_CA_BUNDLE=str(tls / "ca-cert.pem"))

    def check(self) -> subprocess.CompletedProcess[str]:
        result = run_bash(REPO / "bin/watch-primary.sh", self.env)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(len((result.stdout + result.stderr).splitlines()), 1)
        return result

    def alerts(self) -> list:
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_threshold_recovery_and_no_repeats(self) -> None:
        self.check()
        self.assertEqual(self.alerts(), [])
        self.responses["/primary"] = (503, b'{"ok":true}')
        self.check()
        self.assertEqual(self.alerts(), [])
        self.check()
        self.check()
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.alerts()[0][0], "Arachne DOWN")
        self.assertIn(self.config["ARACHNE_PUBLIC_URL"], self.alerts()[0][1])
        self.responses["/primary"] = (200, b'{"ok":true}')
        self.check()
        self.check()
        self.assertEqual([call[0] for call in self.alerts()], ["Arachne DOWN", "Arachne RECOVERED"])
        state = self.runtime / "watch-primary.json"
        self.assertEqual(json.loads(state.read_text()), {"failures": 0, "down": False})
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertFalse(flock_held(self.runtime / "watch-primary.lock"))

    def test_control_failure_freezes_counter_and_down_state(self) -> None:
        self.configure(ARACHNE_WATCH_CONTROL_URL=self.base + "/control")
        self.responses["/primary"] = (500, b'{}')
        self.check()
        state = self.runtime / "watch-primary.json"
        before = state.read_bytes()
        self.responses["/control"] = (500, b'{}')
        for _ in range(3):
            self.assertIn("local network unavailable", self.check().stdout)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(self.alerts(), [])
        self.responses["/control"] = (200, b'{"ok":true}')
        self.check()
        self.assertEqual(len(self.alerts()), 1)
        self.responses["/control"] = (500, b'{}')
        self.check()
        self.assertEqual(len(self.alerts()), 1)
        self.responses["/primary"] = (200, b'{"ok":true}')
        self.check()
        self.assertEqual(self.alerts()[-1][0], "Arachne RECOVERED")

    def test_health_requires_200_and_literal_json_true(self) -> None:
        self.configure(ARACHNE_WATCH_FAILURES="1")
        for response in ((200, b'{"ok":false}'), (200, b'{"ok":1}'),
                         (200, b'{"ok":"true"}'), (200, b'[]'),
                         (200, b'not json'), (201, b'{"ok":true}')):
            with self.subTest(response=response):
                self.responses["/primary"] = response
                self.check()
                self.assertEqual(self.alerts()[-1][0], "Arachne DOWN")
                self.responses["/primary"] = (200, b'{"ok":true}')
                self.check()
                self.assertEqual(self.alerts()[-1][0], "Arachne RECOVERED")

    def test_rejects_plain_http_and_insecure_state(self) -> None:
        self.configure(ARACHNE_PRIMARY_HEALTH_URL="http://127.0.0.1/health")
        result = run_bash(REPO / "bin/watch-primary.sh", self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.runtime.exists())
        self.configure(ARACHNE_PRIMARY_HEALTH_URL=self.base + "/primary")
        self.check()
        (self.runtime / "watch-primary.json").chmod(0o644)
        result = run_bash(REPO / "bin/watch-primary.sh", self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.alerts(), [])

    def test_default_notification_passes_text_as_arguments(self) -> None:
        mock_bin = self.root / "bin"
        mock_bin.mkdir()
        osascript = mock_bin / "osascript"
        shutil.copyfile(self.config["ARACHNE_ALERT_COMMAND"], osascript)
        osascript.chmod(0o700)
        self.env["PATH"] = f"{mock_bin}{os.pathsep}{os.environ['PATH']}"
        self.configure(ARACHNE_ALERT_COMMAND="", ARACHNE_WATCH_FAILURES="1")
        self.responses["/primary"] = (500, b'{}')
        self.check()
        arguments = self.alerts()[0]
        self.assertEqual(arguments[0], "-e")
        self.assertNotIn(self.base, arguments[1])
        self.assertNotIn(self.config["ARACHNE_PUBLIC_URL"], arguments[1])
        self.assertEqual(arguments[2], "Arachne DOWN")
        self.assertIn(self.config["ARACHNE_PUBLIC_URL"], arguments[3])

    def test_connection_failure_is_counted(self) -> None:
        self.configure(ARACHNE_PRIMARY_HEALTH_URL="https://127.0.0.1:0/health",
                       ARACHNE_WATCH_FAILURES="1")
        self.check()
        self.assertEqual(self.alerts()[0][0], "Arachne DOWN")

    def test_lock_contention_preserves_state(self) -> None:
        self.check()
        before = (self.runtime / "watch-primary.json").read_bytes()
        holder = hold_flock(self.runtime / "watch-primary.lock")
        self.responses["/primary"] = (500, b'{}')
        try:
            self.assertIn("locked", self.check().stdout)
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual((self.runtime / "watch-primary.json").read_bytes(), before)
        self.assertEqual(self.alerts(), [])


class BootstrapConfigTests(unittest.TestCase):
    def test_public_endpoint_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token = Path(temporary) / "auth-token"
            token.write_text("A" * 32 + "\n", encoding="ascii")
            environment = os.environ.copy()
            environment.pop("ARACHNE_PUBLIC_URL", None)
            result = subprocess.run(
                [
                    str(REPO / "bin/bootstrap-url.py"),
                    "--token-file",
                    str(token),
                    "decision.html",
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("set ARACHNE_PUBLIC_URL", result.stderr)


class CronSafetyTests(unittest.TestCase):
    def make_crontab(self, root: Path) -> Path:
        script = root / "crontab"
        script.write_text(
            """#!/bin/sh
if [ "$1" = "-l" ]; then
  case "$CRONTAB_MODE" in
    none) echo "no crontab for test" >&2; exit 1 ;;
    error) echo "permission denied" >&2; exit 2 ;;
    current) cat "$CRONTAB_SOURCE"; exit 0 ;;
  esac
fi
cp "$1" "$CRONTAB_OUTPUT"
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_read_error_is_not_treated_as_empty_crontab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_crontab(root)
            output = root / "installed"
            result = run_bash(
                REPO / "bin/install-cron.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "CRONTAB_MODE": "error",
                    "CRONTAB_OUTPUT": str(output),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertIn("refusing to replace", result.stderr)

    def test_unbalanced_managed_markers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_crontab(root)
            source = root / "current"
            source.write_text(
                "# BEGIN ARACHNE (managed by bin/install-cron.sh)\n* * * * * old\n",
                encoding="utf-8",
            )
            output = root / "installed"
            result = run_bash(
                REPO / "bin/install-cron.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "CRONTAB_MODE": "current",
                    "CRONTAB_SOURCE": str(source),
                    "CRONTAB_OUTPUT": str(output),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertIn("ambiguous crontab", result.stderr)


class KeepaliveConfigTests(unittest.TestCase):
    def test_missing_default_deployment_environment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bash(
                REPO / "keepalive.sh",
                {**os.environ, "HOME": temporary},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deployment environment is missing", result.stderr)

    def test_quiesce_sentinel_stops_before_external_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "QUIESCED").touch()
            deploy = root / "deployment.env"
            deploy.write_text(f"ARACHNE_RUNTIME_DIR={runtime}\n", encoding="utf-8")
            deploy.chmod(0o600)
            result = run_bash(
                REPO / "keepalive.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "ARACHNE_DEPLOY_ENV": str(deploy),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_group_readable_deployment_environment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            deployment_env = Path(temporary) / "deployment.env"
            deployment_env.write_text("ARACHNE_PORT=8788\n", encoding="ascii")
            deployment_env.chmod(0o640)
            result = run_bash(
                REPO / "keepalive.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "ARACHNE_DEPLOY_ENV": str(deployment_env),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deny group/other access", result.stderr)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required")
    def test_python_wrapper_is_resolved_before_exact_process_matching(self) -> None:
        bundle = system_ca_bundle()
        if bundle is None:
            self.skipTest("system CA bundle is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mock_bin = root / "bin"
            mock_bin.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            data = root / "data"
            resolved_python = mock_bin / "python-real"
            resolved_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            resolved_python.chmod(0o755)
            python_wrapper = mock_bin / "python"
            python_wrapper.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then\n"
                "  printf '%s\\n' \"$ARACHNE_RESOLVED_PYTHON\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 98\n",
                encoding="utf-8",
            )
            python_wrapper.chmod(0o755)
            expected = f"{resolved_python} {REPO}/server.py"
            (mock_bin / "ps").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$ARACHNE_EXPECTED_COMMAND\"\n",
                encoding="utf-8",
            )
            (mock_bin / "curl").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$ARACHNE_CURL_LOG\"\n"
                "printf 'curl %s\\n' \"$*\" >>\"$ARACHNE_EVENT_LOG\"\n",
                encoding="utf-8",
            )
            (mock_bin / "tailscale").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$ARACHNE_TS_LOG\"\n"
                "printf 'tailscale %s\\n' \"$*\" >>\"$ARACHNE_EVENT_LOG\"\n",
                encoding="utf-8",
            )
            for name in ("ps", "curl", "tailscale"):
                (mock_bin / name).chmod(0o755)
            (runtime / "server.pid").write_text(f"{os.getpid()}\n", encoding="ascii")
            deploy = root / "deployment.env"
            deploy.write_text(
                "\n".join(
                    [
                        "ARACHNE_MANAGE_TAILSCALED=false",
                        f"ARACHNE_RUNTIME_DIR={runtime}",
                        f"ARACHNE_DATA_DIR={data}",
                        f"ARACHNE_PYTHON={python_wrapper}",
                        f"TAILSCALE_BIN={mock_bin / 'tailscale'}",
                        "TAILSCALE_SOCKET=/run/tailscale/tailscaled.sock",
                        f"ARACHNE_SYSTEM_CA_BUNDLE={bundle}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            deploy.chmod(0o600)
            curl_log = root / "curl.log"
            tailscale_log = root / "tailscale.log"
            event_log = root / "events.log"
            result = run_bash(
                REPO / "keepalive.sh",
                {
                    **os.environ,
                    "HOME": temporary,
                    "PATH": f"{mock_bin}{os.pathsep}{os.environ['PATH']}",
                    "ARACHNE_DEPLOY_ENV": str(deploy),
                    "ARACHNE_EXPECTED_COMMAND": expected,
                    "ARACHNE_RESOLVED_PYTHON": str(resolved_python),
                    "ARACHNE_CURL_LOG": str(curl_log),
                    "ARACHNE_TS_LOG": str(tailscale_log),
                    "ARACHNE_EVENT_LOG": str(event_log),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            curl_arguments = curl_log.read_text(encoding="utf-8")
            self.assertIn("--cacert", curl_arguments)
            self.assertIn("https://127.0.0.1:8788/health", curl_arguments)
            tailscale_arguments = tailscale_log.read_text(encoding="utf-8")
            self.assertIn("status", tailscale_arguments)
            self.assertIn("serve --bg https://localhost:8788", tailscale_arguments)
            self.assertIn("--socket=/run/tailscale/tailscaled.sock", tailscale_arguments)
            events = event_log.read_text(encoding="utf-8").splitlines()
            first_serve = next(index for index, line in enumerate(events) if " serve " in line)
            first_health = next(index for index, line in enumerate(events) if line.startswith("curl "))
            self.assertLess(first_serve, first_health)


if __name__ == "__main__":
    unittest.main()
