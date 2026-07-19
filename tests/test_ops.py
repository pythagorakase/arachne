from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import tempfile
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
    def test_system_daemon_mode_uses_verified_https_backend(self) -> None:
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
            fake_python = mock_bin / "python"
            fake_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake_python.chmod(0o755)
            expected = f"{fake_python} {REPO}/server.py"
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
                        f"ARACHNE_PYTHON={fake_python}",
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
