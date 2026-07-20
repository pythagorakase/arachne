from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request, urlopen

import page_contract
import server as arachne_server


REPO = Path(__file__).resolve().parents[1]


def axis_manifest(issue: str) -> dict:
    return {
        "contract": "v2",
        "issue": issue,
        "title": f"Decision {issue}",
        "overall_notes": False,
        "axes": [
            {
                "id": "choice",
                "label": "Choice",
                "select": "one",
                "notes": False,
                "options": [{"id": "accept", "label": "Accept"}],
            }
        ],
    }


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind((arachne_server.LOOPBACK_HOST, 0))
        return candidate.getsockname()[1]


class RunningServer:
    def __init__(
        self,
        root: Path,
        *,
        tls_cert_file: Path | None = None,
        tls_key_file: Path | None = None,
    ) -> None:
        self.pages = root / "pages"
        self.data = root / "data"
        self.pages.mkdir(parents=True)
        (self.pages / "decision.html").write_text(
            """<!doctype html><style>body{color:navy}</style>
<img src="data:image/png;base64,AA=="><script>
localStorage.setItem('draft', 'yes'); fetch('/ruling');
</script>""",
            encoding="utf-8",
        )
        self.port = free_port()
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.process: subprocess.Popen[str] | None = None
        self.startup: dict[str, object] | None = None

    @property
    def scheme(self) -> str:
        return "https" if self.tls_cert_file is not None else "http"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{arachne_server.LOOPBACK_HOST}:{self.port}"

    @property
    def token(self) -> str:
        return (self.data / "auth-token").read_text(encoding="ascii").strip()

    def verification_context(self) -> ssl.SSLContext | None:
        if self.tls_cert_file is None:
            return None
        return ssl.create_default_context(cafile=str(self.tls_cert_file))

    def start(self) -> None:
        environment = os.environ.copy()
        environment.pop("ARACHNE_TLS_CERT_FILE", None)
        environment.pop("ARACHNE_TLS_KEY_FILE", None)
        environment.update(
            {
                "ARACHNE_PAGES_DIR": str(self.pages),
                "ARACHNE_DATA_DIR": str(self.data),
                "ARACHNE_PORT": str(self.port),
                "ARACHNE_WAIT_SECONDS": "0.1",
                "ARACHNE_LOG_LEVEL": "WARNING",
            }
        )
        if self.tls_cert_file is not None and self.tls_key_file is not None:
            environment.update(
                {
                    "ARACHNE_TLS_CERT_FILE": str(self.tls_cert_file),
                    "ARACHNE_TLS_KEY_FILE": str(self.tls_key_file),
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
        context = self.verification_context()
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise AssertionError(
                    f"server exited during startup\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                with urlopen(f"{self.url}/health", context=context, timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("server did not become healthy")
        assert self.process.stdout is not None
        self.startup = json.loads(self.process.stdout.readline())

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


def raw_exchange(port: int, request: bytes, *, half_close: bool = False) -> bytes:
    with socket.create_connection((arachne_server.LOOPBACK_HOST, port), timeout=2) as client:
        client.settimeout(2)
        client.sendall(request)
        if half_close:
            client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


class AuthenticationExpiryTests(unittest.TestCase):
    def test_session_cookie_has_authenticated_server_side_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authentication = arachne_server.Authentication(
                Path(directory) / "auth-token"
            )
            issued_at = 1_700_000_000
            with patch.object(arachne_server.time, "time", return_value=issued_at):
                header = authentication.session_cookie(secure=True)

            cookies = SimpleCookie()
            cookies.load(header)
            value = cookies[arachne_server.SESSION_COOKIE_NAME].value
            request_cookie = f"{arachne_server.SESSION_COOKIE_NAME}={value}"
            with patch.object(
                arachne_server.time,
                "time",
                return_value=issued_at + arachne_server.SESSION_COOKIE_SECONDS - 1,
            ):
                self.assertTrue(authentication.accepts_cookie(request_cookie))
            with patch.object(
                arachne_server.time,
                "time",
                return_value=issued_at + arachne_server.SESSION_COOKIE_SECONDS,
            ):
                self.assertFalse(authentication.accepts_cookie(request_cookie))

            replacement = "0" if value[-1] != "0" else "1"
            tampered = f"{arachne_server.SESSION_COOKIE_NAME}={value[:-1]}{replacement}"
            with patch.object(arachne_server.time, "time", return_value=issued_at):
                self.assertFalse(authentication.accepts_cookie(tampered))
                self.assertFalse(
                    authentication.accepts_cookie(
                        f'{arachne_server.SESSION_COOKIE_NAME}="v1.9999999999.\\377"'
                    )
                )
            self.assertIn(f"Max-Age={arachne_server.SESSION_COOKIE_SECONDS}", header)
            self.assertIn("Secure", header)
            self.assertIn("HttpOnly", header)
            self.assertIn("SameSite=Strict", header)

    def test_tls_environment_requires_a_nonempty_pair(self) -> None:
        for environment in (
            {"ARACHNE_TLS_CERT_FILE": "/tmp/cert.pem"},
            {"ARACHNE_TLS_KEY_FILE": "/tmp/key.pem"},
            {"ARACHNE_TLS_CERT_FILE": "", "ARACHNE_TLS_KEY_FILE": ""},
        ):
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaisesRegex(ValueError, "ARACHNE_TLS_"):
                    arachne_server.Config.from_environment()


class ServerProtocolHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = RunningServer(Path(self.temporary.name))
        self.service.start()

    def tearDown(self) -> None:
        self.service.stop()
        self.temporary.cleanup()

    def test_decision_page_has_restrictive_compatible_csp(self) -> None:
        request = Request(
            f"{self.service.url}/decision.html",
            headers={"Authorization": f"Bearer {self.service.token}"},
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(
                response.headers["Content-Security-Policy"],
                arachne_server.DECISION_PAGE_CSP,
            )
            self.assertIn(b"localStorage", response.read())
        policy = arachne_server.DECISION_PAGE_CSP
        for directive in (
            "script-src 'unsafe-inline'",
            "style-src 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-src 'none'",
            "frame-ancestors 'self'",
        ):
            self.assertIn(directive, policy)
        self.assertIn("default-src 'none'", policy)

    def test_rejected_post_closes_without_parsing_pipelined_request(self) -> None:
        body = b"{}"
        request = (
            b"POST /ruling HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n"
            + body
            + b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        response = raw_exchange(self.service.port, request)
        self.assertIn(b"HTTP/1.1 401 Unauthorized", response)
        self.assertIn(b"Connection: close", response)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertNotIn(b'"ok": true', response)

    def test_get_body_is_rejected_without_parsing_pipelined_bytes(self) -> None:
        smuggled = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        request = (
            b"GET /health HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + f"Content-Length: {len(smuggled)}\r\n\r\n".encode("ascii")
            + smuggled
        )
        response = raw_exchange(self.service.port, request)
        self.assertIn(b"HTTP/1.1 400 Bad Request", response)
        self.assertIn(b'"error": "unexpected_request_body"', response)
        self.assertIn(b"Connection: close", response)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)

    def test_oversized_decimal_content_lengths_are_bounded_client_errors(self) -> None:
        huge_length = b"9" * 5000
        get_response = raw_exchange(
            self.service.port,
            b"GET /health HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + huge_length
            + b"\r\n\r\n",
        )
        self.assertIn(b"HTTP/1.1 400 Bad Request", get_response)
        self.assertIn(b'"error": "unexpected_request_body"', get_response)
        self.assertIn(b"Connection: close", get_response)

        post_response = raw_exchange(
            self.service.port,
            b"POST /session HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: "
            + huge_length
            + b"\r\n\r\n",
        )
        self.assertIn(b"HTTP/1.1 413 ", post_response)
        self.assertIn(b'"error": "payload_too_large"', post_response)
        self.assertIn(b"Connection: close", post_response)

    def test_short_content_length_body_is_a_client_error_and_closes(self) -> None:
        request = (
            b"POST /session HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n{}"
        )
        response = raw_exchange(self.service.port, request, half_close=True)
        self.assertIn(b"HTTP/1.1 400 Bad Request", response)
        self.assertIn(b'"error": "incomplete_body"', response)
        self.assertIn(b"Connection: close", response)

    def test_duplicate_content_length_is_rejected(self) -> None:
        token = self.service.token.encode("ascii")
        request = (
            b"POST /ruling HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer "
            + token
            + b"\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
        )
        response = raw_exchange(self.service.port, request)
        self.assertIn(b"HTTP/1.1 400 Bad Request", response)
        self.assertIn(b'"error": "invalid_length"', response)
        self.assertIn(b"Connection: close", response)

    def test_body_read_timeout_is_a_408_client_error(self) -> None:
        root = Path(self.temporary.name) / "timeout-server"
        pages = root / "pages"
        data = root / "data"
        pages.mkdir(parents=True)
        config = arachne_server.Config(
            pages_dir=pages,
            data_dir=data,
            token_file=data / "auth-token",
            port=0,
            wait_seconds=0.1,
            secure_cookie=False,
            tls_cert_file=None,
            tls_key_file=None,
        )
        authentication = arachne_server.Authentication(config.token_file)
        store = arachne_server.RulingStore(config.data_dir)
        with patch.object(arachne_server.ArachneHandler, "timeout", 0.1):
            timeout_server = arachne_server.ArachneServer(
                config, store, authentication
            )
            thread = threading.Thread(target=timeout_server.serve_forever, daemon=True)
            thread.start()
            try:
                request = (
                    b"POST /session HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n{}"
                )
                response = raw_exchange(timeout_server.server_port, request)
            finally:
                timeout_server.shutdown()
                timeout_server.server_close()
                thread.join(timeout=2)
        self.assertIn(b"HTTP/1.1 408 Request Timeout", response)
        self.assertIn(b'"error": "request_timeout"', response)
        self.assertIn(b"Connection: close", response)

    def test_connection_workers_are_bounded_before_thread_creation(self) -> None:
        root = Path(self.temporary.name) / "bounded-server"
        pages = root / "pages"
        data = root / "data"
        pages.mkdir(parents=True)
        config = arachne_server.Config(
            pages_dir=pages,
            data_dir=data,
            token_file=data / "auth-token",
            port=0,
            wait_seconds=0.1,
            secure_cookie=False,
            tls_cert_file=None,
            tls_key_file=None,
        )
        authentication = arachne_server.Authentication(config.token_file)
        store = arachne_server.RulingStore(config.data_dir)
        with patch.object(arachne_server, "MAX_CONNECTION_WORKERS", 1):
            bounded_server = arachne_server.ArachneServer(
                config, store, authentication
            )
        thread = threading.Thread(target=bounded_server.serve_forever, daemon=True)
        thread.start()
        first = socket.create_connection(
            (arachne_server.LOOPBACK_HOST, bounded_server.server_port), timeout=2
        )
        try:
            first.sendall(b"G")
            time.sleep(0.1)
            with socket.create_connection(
                (arachne_server.LOOPBACK_HOST, bounded_server.server_port), timeout=2
            ) as rejected:
                rejected.settimeout(1)
                try:
                    rejected.sendall(
                        b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    )
                    response = rejected.recv(4096)
                except (BrokenPipeError, ConnectionResetError):
                    response = b""
            self.assertNotIn(b"HTTP/1.1 200 OK", response)
        finally:
            first.close()
            bounded_server.shutdown()
            bounded_server.server_close()
            thread.join(timeout=2)


class TLSServerTests(unittest.TestCase):
    def _create_tls_service(self, root: Path) -> RunningServer:
        certificate = root / "loopback-cert.pem"
        private_key = root / "loopback-key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(private_key),
                "-out",
                str(certificate),
            ],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return RunningServer(
            root / "service",
            tls_cert_file=certificate,
            tls_key_file=private_key,
        )

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS fixture")
    def test_real_tls_server_is_verified_and_reports_tls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._create_tls_service(root)
            try:
                service.start()
                self.assertEqual(service.startup["tls"], True)
                with urlopen(
                    f"{service.url}/health",
                    context=service.verification_context(),
                    timeout=2,
                ) as response:
                    health = json.load(response)
                self.assertEqual(health["tls"], True)
                self.assertEqual(health["bound_host"], "127.0.0.1")
            finally:
                service.stop()

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS fixture")
    def test_untrusted_backend_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._create_tls_service(Path(directory))
            try:
                service.start()
                with self.assertRaises(URLError):
                    urlopen(
                        f"{service.url}/health",
                        context=ssl.create_default_context(),
                        timeout=2,
                    )
            finally:
                service.stop()

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS fixture")
    def test_idle_raw_peer_does_not_block_verified_https_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._create_tls_service(Path(directory))
            try:
                service.start()
                with socket.create_connection(
                    (arachne_server.LOOPBACK_HOST, service.port), timeout=2
                ):
                    # Give the accept loop time to dispatch the idle connection's
                    # blocked handshake before opening a second connection.
                    time.sleep(0.1)
                    with urlopen(
                        f"{service.url}/health",
                        context=service.verification_context(),
                        timeout=2,
                    ) as response:
                        health = json.load(response)
                self.assertEqual(health["ok"], True)
                self.assertEqual(health["tls"], True)
            finally:
                service.stop()


class PublicationTransactionTests(unittest.TestCase):
    def test_reserved_docket_option_ids_are_rejected(self) -> None:
        for sentinel in sorted(page_contract.RESERVED_OPTION_IDS):
            with self.subTest(sentinel=sentinel):
                manifest = axis_manifest(f"reserved-{sentinel}")
                manifest["axes"][0]["options"][0]["id"] = sentinel
                with self.assertRaisesRegex(ValueError, "reserved docket sentinel"):
                    page_contract.validate_axes_manifest(manifest)

    def test_v2_manifest_is_normalized_stored_and_read_from_the_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pages = Path(temporary)
            manifest = axis_manifest("sidecar")
            manifest.pop("overall_notes")
            publication = page_contract.publish_html(
                "decision_sidecar.html",
                "<!doctype html><main>Argument only</main>",
                pages,
                manifest,
            )

            expected = {**manifest, "overall_notes": False}
            self.assertEqual(publication.issue, "sidecar")
            self.assertEqual(
                page_contract.read_page_issue(pages, "decision_sidecar.html"),
                "sidecar",
            )
            self.assertEqual(
                page_contract.read_page_axes(pages, "decision_sidecar.html"),
                expected,
            )
            sidecar = json.loads(
                page_contract.metadata_path(
                    pages, "decision_sidecar.html"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(sidecar, {"issue": "sidecar", "axes": expected})

    def test_concurrent_same_name_publications_never_interleave(self) -> None:
        # PR #10 second-review P1: page and sidecar are two commits; racing
        # same-name publications must never pair one publication's page with
        # another's issue.
        with tempfile.TemporaryDirectory() as temporary:
            pages = Path(temporary)
            errors: list[Exception] = []

            def publish_variant(marker: str) -> None:
                html = (
                    f"<!doctype html><p>variant {marker}</p>"
                )
                try:
                    for _ in range(80):
                        page_contract.publish_html(
                            "decision_race.html",
                            html,
                            pages,
                            axis_manifest(marker),
                            issue=marker,
                        )
                except Exception as exc:  # noqa: BLE001 - surfaced via assert
                    errors.append(exc)

            threads = [
                threading.Thread(target=publish_variant, args=(marker,))
                for marker in ("alpha", "beta")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            recorded = page_contract.read_page_issue(pages, "decision_race.html")
            self.assertIn(recorded, {"alpha", "beta"})
            axes = page_contract.read_page_axes(pages, "decision_race.html")
            self.assertIsNotNone(axes)
            assert axes is not None
            self.assertEqual(axes["issue"], recorded)
            final_html = (pages / "decision_race.html").read_text(encoding="utf-8")
            self.assertIn(f"variant {recorded}", final_html)


if __name__ == "__main__":
    unittest.main()
