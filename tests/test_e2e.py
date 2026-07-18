from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


class RunningArachne:
    def __init__(self, pages: Path, data: Path, wait_seconds: float = 0.5) -> None:
        self.pages = pages
        self.data = data
        self.wait_seconds = wait_seconds
        self.port = free_port()
        self.process: subprocess.Popen[str] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "ARACHNE_PAGES_DIR": str(self.pages),
                "ARACHNE_DATA_DIR": str(self.data),
                "ARACHNE_PORT": str(self.port),
                "ARACHNE_WAIT_SECONDS": str(self.wait_seconds),
                "ARACHNE_LOG_LEVEL": "WARNING",
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, str(REPO / "server.py")],
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise AssertionError(
                    f"server exited during startup\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                with urlopen(f"{self.url}/health", timeout=0.2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.02)
        raise AssertionError("server did not become healthy")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.communicate()
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.port = free_port()
        self.start()


def get_json(url: str, timeout: float = 3) -> tuple[int, dict]:
    with urlopen(url, timeout=timeout) as response:
        data = response.read()
        return response.status, json.loads(data) if data else {}


def post_ruling(url: str, issue: str = "476") -> tuple[int, dict]:
    payload = json.dumps(
        {
            "issue": issue,
            "markdown": f"# Ruling {issue}\n\nChoose the woven path.",
            "form": {"choice": "woven", "confidence": 0.9},
        }
    ).encode()
    request = Request(
        f"{url}/ruling",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return response.status, json.load(response)


class ArachneEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.pages = root / "pages"
        self.data = root / "data"
        self.pages.mkdir()
        (self.pages / "decision_476.html").write_text(
            "<!doctype html><title>Arachne test</title><h1>Choose</h1>",
            encoding="utf-8",
        )
        self.service = RunningArachne(self.pages, self.data)
        self.service.start()

    def tearDown(self) -> None:
        self.service.stop()
        self.temporary.cleanup()

    def test_health_page_allowlist_and_hardening(self) -> None:
        status, health = get_json(f"{self.service.url}/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["bound_host"], "127.0.0.1")

        with urlopen(f"{self.service.url}/decision_476.html") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Arachne test", response.read())

        for path in ("/", "/unknown.html", "/%2e%2e/SPEC.md", "/pages/"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{self.service.url}{path}")
                self.assertEqual(raised.exception.code, 404)

    def test_file_ruling_persists_both_artifacts(self) -> None:
        status, entry = post_ruling(self.service.url)
        self.assertEqual(status, 201)
        self.assertEqual(entry["sequence"], 1)
        self.assertEqual(entry["form"]["choice"], "woven")
        ruling_dir = self.data / "rulings"
        json_files = list(ruling_dir.glob("*.json"))
        markdown_files = list(ruling_dir.glob("*.md"))
        self.assertEqual(len(json_files), 1)
        self.assertEqual(len(markdown_files), 1)
        self.assertEqual(json.loads(json_files[0].read_text())["sequence"], 1)
        self.assertIn("Choose the woven path", markdown_files[0].read_text())

    def test_push_wake_and_parked_waiter_do_not_block_requests(self) -> None:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            started = time.monotonic()
            waiter = pool.submit(
                get_json, f"{self.service.url}/wait?since=0", 3
            )
            time.sleep(0.08)
            with urlopen(f"{self.service.url}/decision_476.html", timeout=1) as page:
                self.assertEqual(page.status, 200)
            status, filed = post_ruling(self.service.url)
            wait_status, received = waiter.result(timeout=2)
        self.assertEqual(status, 201)
        self.assertEqual(wait_status, 200)
        self.assertEqual(received["sequence"], filed["sequence"])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_wake_client_exits_with_ruling_and_advances_cursor(self) -> None:
        cursor_file = Path(self.temporary.name) / "agent-state" / "cursor"
        environment = os.environ.copy()
        environment.update(
            {
                "ARACHNE_URL": self.service.url,
                "ARACHNE_CURSOR_FILE": str(cursor_file),
                "ARACHNE_REQUEST_TIMEOUT": "3",
            }
        )
        waiter = subprocess.Popen(
            [str(REPO / "bin" / "arm-wake.sh")],
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.08)
            _, filed = post_ruling(self.service.url, "wake-client")
            stdout, stderr = waiter.communicate(timeout=2)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.wait(timeout=2)
        self.assertEqual(waiter.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["sequence"], filed["sequence"])
        self.assertEqual(cursor_file.read_text().strip(), str(filed["sequence"]))

    def test_missed_wake_returns_immediately(self) -> None:
        _, filed = post_ruling(self.service.url)
        started = time.monotonic()
        status, received = get_json(f"{self.service.url}/wait?since=0")
        self.assertEqual(status, 200)
        self.assertEqual(received["sequence"], filed["sequence"])
        self.assertLess(time.monotonic() - started, 0.25)

    def test_restart_reconstructs_cursor_and_returns_next_ruling(self) -> None:
        _, first = post_ruling(self.service.url, "first")
        self.service.restart()
        status, health = get_json(f"{self.service.url}/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["latest_sequence"], first["sequence"])
        _, second = post_ruling(self.service.url, "second")
        status, received = get_json(
            f"{self.service.url}/wait?since={first['sequence']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(received["sequence"], second["sequence"])

    def test_wait_timeout_is_204(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.service.port, timeout=2)
        connection.request("GET", "/wait?since=0")
        response = connection.getresponse()
        self.assertEqual(response.status, 204)
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_ruling_validation_fails_loud(self) -> None:
        request = Request(
            f"{self.service.url}/ruling",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        problem = json.loads(raised.exception.read())
        self.assertEqual(problem["error"], "invalid_ruling")
        self.assertTrue(problem["detail"])

    def test_publisher_rewrites_loopback_and_enforces_page_contract(self) -> None:
        source = Path(self.temporary.name) / "decision_publish.html"
        destination_dir = Path(self.temporary.name) / "published"
        source.write_text(
            """<!doctype html><script>
            localStorage.setItem('draft', 'yes');
            fetch('http://127.0.0.1:8788/ruling', {method: 'POST'});
            </script>""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                "--pages-dir",
                str(destination_dir),
                str(source),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        published = (destination_dir / source.name).read_text()
        self.assertIn("fetch('/ruling'", published)
        self.assertNotIn("127.0.0.1", published)


if __name__ == "__main__":
    unittest.main(verbosity=2)
