from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
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
from urllib.parse import parse_qs, urlsplit
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

    @property
    def token_file(self) -> Path:
        return self.data / "auth-token"

    @property
    def token(self) -> str:
        return self.token_file.read_text(encoding="ascii").strip()

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "ARACHNE_PAGES_DIR": str(self.pages),
                "ARACHNE_DATA_DIR": str(self.data),
                "ARACHNE_PORT": str(self.port),
                "ARACHNE_WAIT_SECONDS": str(self.wait_seconds),
                "ARACHNE_LOG_LEVEL": "WARNING",
                "ARACHNE_SECURE_COOKIE": "true",
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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def craft_session_cookie(token: str, expires_at: int) -> str:
    """Reproduce the server's signed cookie for expiry-window tests."""

    signature = hmac.new(
        token.encode("ascii"),
        f"arachne-browser-session-v1:{expires_at}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"arachne_session=v1.{expires_at}.{signature}"


def get_json(
    url: str, timeout: float = 3, token: str | None = None
) -> tuple[int, dict]:
    request = Request(url, headers=bearer(token) if token else {})
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
        return response.status, json.loads(data) if data else {}


def post_ruling(url: str, token: str, issue: str = "476") -> tuple[int, dict]:
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
        headers={"Content-Type": "application/json", **bearer(token)},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return response.status, json.load(response)


def post_json(url: str, path: str, token: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    request = Request(
        f"{url}{path}",
        data=body,
        headers={"Content-Type": "application/json", **bearer(token)},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        data = response.read()
        return response.status, json.loads(data) if data else {}


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

        with urlopen(
            Request(
                f"{self.service.url}/decision_476.html",
                headers=bearer(self.service.token),
            )
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Arachne test", response.read())

        for path in ("/unknown.html", "/%2e%2e/SPEC.md", "/pages/"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        Request(
                            f"{self.service.url}{path}",
                            headers=bearer(self.service.token),
                        )
                    )
                self.assertEqual(raised.exception.code, 404)

    def test_shared_host_authentication_and_browser_bootstrap(self) -> None:
        unauthenticated_requests = (
            Request(f"{self.service.url}/decision_476.html"),
            Request(f"{self.service.url}/wait?since=0"),
            Request(f"{self.service.url}/rulings?since=0"),
            Request(f"{self.service.url}/rulings/1"),
            Request(
                f"{self.service.url}/pages",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            Request(
                f"{self.service.url}/bootstrap-ticket",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            Request(
                f"{self.service.url}/ruling",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
        )
        for request in unauthenticated_requests:
            with self.subTest(path=request.full_url):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=1)
                self.assertEqual(raised.exception.code, 401)

        with urlopen(
            f"{self.service.url}/bootstrap?next=decision_476.html", timeout=1
        ) as response:
            bootstrap = response.read().decode()
        self.assertIn("POST", bootstrap)
        self.assertNotIn(self.service.token, bootstrap)

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.service.port, timeout=2
        )
        session_body = json.dumps({"token": self.service.token})
        connection.request(
            "POST",
            "/session",
            body=session_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(session_body)),
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 204)
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertIsNone(response.getheader("Content-Type"))
        set_cookie = response.getheader("Set-Cookie")
        self.assertIsNotNone(set_cookie)
        assert set_cookie is not None
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertNotIn(self.service.token, set_cookie)
        self.assertEqual(self.service.token_file.stat().st_mode & 0o777, 0o600)
        response.read()
        connection.close()

        cookie = set_cookie.split(";", 1)[0]
        request = Request(
            f"{self.service.url}/decision_476.html", headers={"Cookie": cookie}
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Arachne test", response.read())

        ruling = json.dumps(
            {
                "issue": "browser-cookie",
                "markdown": "# Cookie-authenticated ruling",
                "form": {"choice": "authenticated"},
            }
        ).encode()
        request = Request(
            f"{self.service.url}/ruling",
            data=ruling,
            headers={"Content-Type": "application/json", "Cookie": cookie},
            method="POST",
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 201)
            acknowledgement = json.load(response)
        self.assertIs(acknowledgement["ok"], True)
        self.assertEqual(
            acknowledgement["filed"],
            acknowledgement["artifacts"]["markdown"],
        )
        self.assertEqual(acknowledgement["issue"], "browser-cookie")

        for path, payload in (
            ("/pages", {"name": "decision_cookie.html", "html": "ignored"}),
            ("/bootstrap-ticket", {"page": "decision_476.html"}),
        ):
            body = json.dumps(payload).encode()
            request = Request(
                f"{self.service.url}{path}",
                data=body,
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=1)
            self.assertEqual(raised.exception.code, 401)

    def test_bearer_publication_and_one_time_bootstrap_ticket(self) -> None:
        source = """<!doctype html><title>Argument only</title>
        <main><h1>Choose a path</h1><p>Compare the two approaches.</p></main>"""
        status, published = post_json(
            self.service.url,
            "/pages",
            self.service.token,
            {"name": "decision_mcp.html", "html": source, "issue": "mcp-476"},
        )
        self.assertEqual(status, 201)
        self.assertIs(published["ok"], True)
        self.assertEqual(published["page"], "decision_mcp.html")
        self.assertEqual(published["issue"], "mcp-476")
        self.assertNotIn("rewritten_references", published)
        self.assertNotIn(str(self.pages), json.dumps(published))

        with urlopen(
            Request(
                f"{self.service.url}/decision_mcp.html",
                headers=bearer(self.service.token),
            )
        ) as response:
            html = response.read().decode()
        self.assertEqual(html, source)
        self.assertNotIn("/ruling", html)
        self.assertNotIn("localStorage", html)

        status, bootstrap = post_json(
            self.service.url,
            "/bootstrap-ticket",
            self.service.token,
            {"page": "decision_mcp.html"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(bootstrap["page"], "decision_mcp.html")
        self.assertNotEqual(bootstrap["ticket"], self.service.token)
        self.assertNotIn(self.service.token, json.dumps(bootstrap))

        session_body = json.dumps(
            {"ticket": bootstrap["ticket"], "page": "decision_mcp.html"}
        ).encode()
        request = Request(
            f"{self.service.url}/session",
            data=session_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 204)
            self.assertIn("arachne_session=", response.headers["Set-Cookie"])

        with self.assertRaises(HTTPError) as reused:
            urlopen(request, timeout=1)
        self.assertEqual(reused.exception.code, 401)

        with self.assertRaises(HTTPError) as invalid_page:
            post_json(
                self.service.url,
                "/pages",
                self.service.token,
                {
                    "name": "decision_invalid.html",
                    "html": "<script>localStorage.clear()</script>",
                    "issue": "invalid",
                },
            )
        self.assertEqual(invalid_page.exception.code, 400)
        self.assertFalse((self.pages / "decision_invalid.html").exists())

    def test_manifest_endpoint_is_gone_and_page_csp_is_unchanged(self) -> None:
        name = "decision_parts.html"
        post_json(
            self.service.url,
            "/pages",
            self.service.token,
            {
                "name": name,
                "html": "<!doctype html><title>Parts decision</title><main>Argument only</main>",
                "issue": "parts-issue",
            },
        )

        with self.assertRaises(HTTPError) as absent:
            get_json(f"{self.service.url}/axes/{name}", token=self.service.token)
        self.assertEqual(absent.exception.code, 404)

        with urlopen(
            Request(
                f"{self.service.url}/{name}",
                headers=bearer(self.service.token),
            ),
            timeout=1,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Argument only", response.read())
            page_policy = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", page_policy)
        self.assertIn("frame-ancestors 'self'", page_policy)

        status, inbox, _ = self._get_inbox(bearer(self.service.token))
        self.assertEqual(status, 200)
        self.assertIn('data-part-count="0"', inbox)
        self.assertIn("Parts decision", inbox)
        self.assertIn('class="ruling-nav"', inbox)
        self.assertNotIn("/axes/", inbox)

        cookie = craft_session_cookie(self.service.token, int(time.time()) + 3600)
        with self.assertRaises(HTTPError) as cookie_absent:
            urlopen(
                Request(
                    f"{self.service.url}/axes/{name}",
                    headers={"Cookie": cookie},
                ),
                timeout=1,
            )
        self.assertEqual(cookie_absent.exception.code, 404)

    def _get_inbox(self, headers: dict[str, str]) -> tuple[int, str, dict]:
        request = Request(f"{self.service.url}/", headers=headers)
        with urlopen(request, timeout=2) as response:
            return response.status, response.read().decode(), response.headers

    def test_inbox_lists_pending_briefs_and_archives_on_ruling(self) -> None:
        status, body, headers = self._get_inbox(bearer(self.service.token))
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        policy = headers["Content-Security-Policy"]
        for directive in (
            "default-src 'none'",
            "script-src 'unsafe-inline'",
            "connect-src 'self'",
            "frame-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            self.assertIn(directive, policy)
        self.assertIn("decision_476.html", body)
        self.assertIn("Arachne test", body)
        self.assertIn('data-list-count="awaiting">1</span>', body)
        self.assertIn('data-list-count="archive">0</span>', body)
        self.assertIn('class="app-frame" data-arachne-shell', body)
        self.assertIn('class="brief-frame"', body)
        self.assertIn('<nav class="ruling-nav"', body)
        self.assertIn("data-part-outline", body)
        self.assertNotIn("@@ARACHNE_", body)

        post_ruling(self.service.url, self.service.token, "476")
        status, body, _ = self._get_inbox(bearer(self.service.token))
        self.assertIn('data-list-count="awaiting">0</span>', body)
        self.assertIn("The loom is quiet", body)
        self.assertIn('data-list-count="archive">1</span>', body)
        self.assertLess(
            body.index('data-list-panel="archive"'),
            body.index("decision_476.html"),
        )

        # Re-publishing a brief for an already-ruled issue reopens it: the
        # archive is derived from ruling-after-publication, never mutated.
        time.sleep(0.05)
        (self.pages / "decision_476.html").write_text(
            "<!doctype html><title>Round two</title><h1>Again</h1>",
            encoding="utf-8",
        )
        status, body, _ = self._get_inbox(bearer(self.service.token))
        self.assertIn('data-list-count="awaiting">1</span>', body)
        self.assertIn("Round two", body)
        self.assertIn('data-list-count="archive">0</span>', body)

    def test_allowlisted_ui_font_is_authenticated_and_served_as_truetype(self) -> None:
        with self.assertRaises(HTTPError) as unauthenticated:
            urlopen(f"{self.service.url}/ui/fonts/Megrim.ttf", timeout=1)
        self.assertEqual(unauthenticated.exception.code, 401)

        with urlopen(
            Request(
                f"{self.service.url}/ui/fonts/Megrim.ttf",
                headers=bearer(self.service.token),
            ),
            timeout=1,
        ) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "font/ttf")
        self.assertGreater(len(body), 10_000)
        self.assertEqual(body[:4], b"\x00\x01\x00\x00")

    def test_inbox_without_session_is_a_friendly_shell(self) -> None:
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.service.url}/"), timeout=1)
        self.assertEqual(raised.exception.code, 401)
        self.assertIn("text/html", raised.exception.headers["Content-Type"])
        body = raised.exception.read().decode()
        self.assertIn("no live Arachne session", body)
        self.assertNotIn("decision_476.html", body)
        self.assertNotIn("Awaiting", body)

    def test_browser_session_slides_past_half_life(self) -> None:
        import server as arachne_server

        window = arachne_server.SESSION_COOKIE_SECONDS
        self.assertEqual(window, 15 * 24 * 60 * 60)
        now = int(time.time())

        aging = craft_session_cookie(self.service.token, now + 24 * 60 * 60)
        request = Request(
            f"{self.service.url}/decision_476.html", headers={"Cookie": aging}
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 200)
            renewed = response.headers.get("Set-Cookie")
        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertIn(f"Max-Age={window}", renewed)
        self.assertIn("HttpOnly", renewed)

        renewed_pair = renewed.split(";", 1)[0]
        status, body, _ = self._get_inbox({"Cookie": renewed_pair})
        self.assertEqual(status, 200)
        self.assertIn("data-arachne-shell", body)

        fresh = craft_session_cookie(self.service.token, now + window - 5)
        request = Request(
            f"{self.service.url}/decision_476.html", headers={"Cookie": fresh}
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.headers.get("Set-Cookie"))

        request = Request(
            f"{self.service.url}/decision_476.html",
            headers=bearer(self.service.token),
        )
        with urlopen(request, timeout=1) as response:
            self.assertIsNone(response.headers.get("Set-Cookie"))

        expired = craft_session_cookie(self.service.token, now - 5)
        with self.assertRaises(HTTPError) as raised:
            urlopen(
                Request(
                    f"{self.service.url}/decision_476.html",
                    headers={"Cookie": expired},
                ),
                timeout=1,
            )
        self.assertEqual(raised.exception.code, 401)

    def test_inbox_bootstrap_ticket_flow(self) -> None:
        status, minted = post_json(
            self.service.url, "/bootstrap-ticket", self.service.token, {"page": None}
        )
        self.assertEqual(status, 201)
        self.assertIsNone(minted["page"])
        self.assertNotEqual(minted["ticket"], self.service.token)

        status, omitted = post_json(
            self.service.url, "/bootstrap-ticket", self.service.token, {}
        )
        self.assertEqual(status, 201)
        self.assertIsNone(omitted["page"])

        with urlopen(f"{self.service.url}/bootstrap", timeout=1) as response:
            shell = response.read().decode()
        self.assertNotIn(self.service.token, shell)
        self.assertIn('"/"', shell)

        session_body = json.dumps({"ticket": minted["ticket"], "page": "/"}).encode()
        request = Request(
            f"{self.service.url}/session",
            data=session_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 204)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        status, body, _ = self._get_inbox({"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("data-arachne-shell", body)

        with self.assertRaises(HTTPError) as reused:
            urlopen(request, timeout=1)
        self.assertEqual(reused.exception.code, 401)

        # A page-bound ticket must not unlock the inbox.
        _, bound = post_json(
            self.service.url,
            "/bootstrap-ticket",
            self.service.token,
            {"page": "decision_476.html"},
        )
        mismatched = json.dumps({"ticket": bound["ticket"], "page": "/"}).encode()
        with self.assertRaises(HTTPError) as crossed:
            urlopen(
                Request(
                    f"{self.service.url}/session",
                    data=mismatched,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=1,
            )
        self.assertEqual(crossed.exception.code, 401)

    def test_ruling_in_same_millisecond_as_publication_still_archives(self) -> None:
        # submitted_at is millisecond-truncated; a finer-grained page mtime
        # must not make a same-millisecond ruling look pre-publication.
        page = self.pages / "decision_476.html"
        _, filed = post_ruling(self.service.url, self.service.token, "476")
        submitted = filed["submitted_at"]
        base = submitted[:-1]  # strip trailing Z; millisecond ISO timestamp
        from datetime import datetime

        moment = datetime.fromisoformat(base + "+00:00").timestamp()
        # Republish "at" the ruling's millisecond but 600µs later.
        os.utime(page, ns=(int(moment * 1e9), int((moment + 0.0006) * 1e9)))
        status, body, _ = self._get_inbox(bearer(self.service.token))
        self.assertIn('data-list-count="archive">1</span>', body)
        self.assertIn('data-list-count="awaiting">0</span>', body)

    def test_recorded_issue_pairs_ruling_regardless_of_filename(self) -> None:
        # PR #10 review repro (Sol): a contract-valid slug-only page name
        # whose filed issue shares nothing with the filename must still
        # archive when its ruling lands.
        source = "<!doctype html><title>Drift</title><main>Compare paths</main>"
        status, published = post_json(
            self.service.url,
            "/pages",
            self.service.token,
            {
                "name": "decision_relationship_drift.html",
                "html": source,
                "issue": "476",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(published["issue"], "476")

        post_ruling(self.service.url, self.service.token, "476")
        status, body, _ = self._get_inbox(bearer(self.service.token))
        self.assertLess(
            body.index('data-list-panel="archive"'),
            body.index("decision_relationship_drift.html"),
        )

        # Republishing with a new explicit token replaces the old issue metadata;
        # filename inference is irrelevant when the publisher supplies an issue.
        time.sleep(0.05)
        status, republished = post_json(
            self.service.url,
            "/pages",
            self.service.token,
            {
                "name": "decision_relationship_drift.html",
                "html": source,
                "issue": "relationship",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(republished["issue"], "relationship")
        post_ruling(self.service.url, self.service.token, "476")
        status, body, _ = self._get_inbox(bearer(self.service.token))
        self.assertLess(
            body.index("decision_relationship_drift.html"),
            body.index('data-list-panel="archive"'),
        )

        # Sidecar metadata is never servable, and invalid explicit issues fail
        # before either publication file can be committed.
        with self.assertRaises(HTTPError) as unservable:
            urlopen(
                Request(
                    f"{self.service.url}/.meta/decision_relationship_drift.html.json",
                    headers=bearer(self.service.token),
                ),
                timeout=1,
            )
        self.assertEqual(unservable.exception.code, 404)
        with self.assertRaises(HTTPError) as invalid:
            post_json(
                self.service.url,
                "/pages",
                self.service.token,
                {
                    "name": "decision_x.html",
                    "html": source,
                    "issue": {"not": "a token"},
                },
            )
        self.assertEqual(invalid.exception.code, 400)
        problem = json.load(invalid.exception)
        self.assertIn("string or integer", problem["detail"])

    def test_v2_publication_rejects_capture_code_and_removed_manifest_field(self) -> None:
        forbidden_pages = {
            "relative ruling": "<script>fetch('/ruling')</script>",
            "local storage": "<script>localStorage.clear()</script>",
            "numeric loopback": (
                "<script>fetch('http://127.0.0.1:8788/ruling')</script>"
            ),
            "named loopback": "<script>fetch('http://localhost/ruling')</script>",
        }
        for label, html in forbidden_pages.items():
            with self.subTest(forbidden=label), self.assertRaises(
                HTTPError
            ) as rejected:
                post_json(
                    self.service.url,
                    "/pages",
                    self.service.token,
                    {
                        "name": "decision_forbidden.html",
                        "html": html,
                        "issue": "forbidden",
                    },
                )
            self.assertEqual(rejected.exception.code, 400)
            problem = json.load(rejected.exception)
            self.assertEqual(problem["error"], "invalid_page")
            self.assertTrue(problem["detail"])
            self.assertFalse((self.pages / "decision_forbidden.html").exists())

        with self.assertRaises(HTTPError) as rejected:
            post_json(
                self.service.url,
                "/pages",
                self.service.token,
                {
                    "name": "decision_removed.html",
                    "html": "<!doctype html><main>Argument only</main>",
                    "issue": "removed",
                    "axes": {"contract": "v2"},
                },
            )
        self.assertEqual(rejected.exception.code, 400)
        problem = json.load(rejected.exception)
        self.assertEqual(problem["error"], "invalid_page")
        self.assertIn("name and html", problem["detail"])
        self.assertFalse((self.pages / "decision_removed.html").exists())

    def test_bootstrap_url_helper_inbox_variant(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "bootstrap-url.py"),
                "--base-url",
                "https://arachne.example.test",
                "--token-file",
                str(self.service.token_file),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = urlsplit(result.stdout.strip())
        self.assertEqual(parsed.path, "/bootstrap")
        self.assertEqual(parsed.query, "")
        self.assertEqual(parse_qs(parsed.fragment), {"token": [self.service.token]})

    def test_file_ruling_persists_both_artifacts(self) -> None:
        status, entry = post_ruling(self.service.url, self.service.token)
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

    def test_backlog_listing_and_peek_are_non_destructive(self) -> None:
        _, first = post_ruling(self.service.url, self.service.token, "mock-smoke")
        _, second = post_ruling(self.service.url, self.service.token, "real-decision")
        self.service.restart()

        status, backlog = get_json(
            f"{self.service.url}/rulings?since=0", token=self.service.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(backlog["since"], 0)
        self.assertEqual(backlog["latest_sequence"], second["sequence"])
        self.assertEqual(
            backlog["rulings"],
            [
                {
                    "sequence": first["sequence"],
                    "issue": "mock-smoke",
                    "submitted_at": first["submitted_at"],
                },
                {
                    "sequence": second["sequence"],
                    "issue": "real-decision",
                    "submitted_at": second["submitted_at"],
                },
            ],
        )
        self.assertNotIn("markdown", backlog["rulings"][0])
        self.assertNotIn("form", backlog["rulings"][0])

        status, remaining = get_json(
            f"{self.service.url}/rulings?since={first['sequence']}",
            token=self.service.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [summary["sequence"] for summary in remaining["rulings"]],
            [second["sequence"]],
        )

        status, peeked = get_json(
            f"{self.service.url}/rulings/{first['sequence']}",
            token=self.service.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            peeked,
            {key: value for key, value in first.items() if key not in {"ok", "filed"}},
        )

        # Inspection cannot advance or otherwise mutate the wake cursor: asking
        # from the same cursor still returns the first queued ruling.
        status, waited = get_json(
            f"{self.service.url}/wait?since=0", token=self.service.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(waited["sequence"], first["sequence"])

    def test_backlog_inspection_validates_cursor_and_sequence(self) -> None:
        invalid_listing_paths = (
            "/rulings",
            "/rulings?since=",
            "/rulings?since=-1",
            "/rulings?since=not-a-number",
            "/rulings?since=0&since=1",
            "/rulings?since=0&extra=1",
        )
        for path in invalid_listing_paths:
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                get_json(f"{self.service.url}{path}", token=self.service.token)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(json.load(raised.exception)["error"], "invalid_cursor")

        for path in ("/rulings/0", "/rulings/not-a-number", "/rulings/1/extra"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                get_json(f"{self.service.url}{path}", token=self.service.token)
            self.assertEqual(raised.exception.code, 404)

        with self.assertRaises(HTTPError) as raised:
            get_json(f"{self.service.url}/rulings/1", token=self.service.token)
        self.assertEqual(raised.exception.code, 404)

    def test_push_wake_and_parked_waiter_do_not_block_requests(self) -> None:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            started = time.monotonic()
            waiter = pool.submit(
                get_json, f"{self.service.url}/wait?since=0", 3, self.service.token
            )
            time.sleep(0.08)
            with urlopen(
                Request(
                    f"{self.service.url}/decision_476.html",
                    headers=bearer(self.service.token),
                ),
                timeout=1,
            ) as page:
                self.assertEqual(page.status, 200)
            status, filed = post_ruling(self.service.url, self.service.token)
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
                "ARACHNE_TOKEN_FILE": str(self.service.token_file),
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
            _, filed = post_ruling(
                self.service.url, self.service.token, "wake-client"
            )
            stdout, stderr = waiter.communicate(timeout=2)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.wait(timeout=2)
        self.assertEqual(waiter.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["sequence"], filed["sequence"])
        self.assertEqual(cursor_file.read_text().strip(), str(filed["sequence"]))

    def test_missed_wake_returns_immediately(self) -> None:
        _, filed = post_ruling(self.service.url, self.service.token)
        started = time.monotonic()
        status, received = get_json(
            f"{self.service.url}/wait?since=0", token=self.service.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(received["sequence"], filed["sequence"])
        self.assertLess(time.monotonic() - started, 0.25)

    def test_restart_reconstructs_cursor_and_returns_next_ruling(self) -> None:
        _, first = post_ruling(self.service.url, self.service.token, "first")
        self.service.restart()
        status, health = get_json(f"{self.service.url}/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["latest_sequence"], first["sequence"])
        _, second = post_ruling(self.service.url, self.service.token, "second")
        status, received = get_json(
            f"{self.service.url}/wait?since={first['sequence']}",
            token=self.service.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(received["sequence"], second["sequence"])

    def test_wait_timeout_is_204(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.service.port, timeout=2)
        connection.request(
            "GET",
            "/wait?since=0",
            headers=bearer(self.service.token),
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 204)
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertIsNone(response.getheader("Content-Type"))
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_head_health_and_idle_connection_timeout(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.service.port, timeout=2
        )
        connection.request("HEAD", "/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertGreater(int(response.getheader("Content-Length", "0")), 0)
        self.assertEqual(response.read(), b"")
        connection.close()

        import server as arachne_server

        self.assertEqual(arachne_server.ArachneHandler.timeout, 75)

    def test_corrupt_committed_ruling_fails_loud_on_startup(self) -> None:
        root = Path(self.temporary.name) / "corrupt-startup"
        pages = root / "pages"
        data = root / "data"
        pages.mkdir(parents=True)
        rulings = data / "rulings"
        rulings.mkdir(parents=True)
        (rulings / "broken.json").write_text("{not json", encoding="utf-8")
        service = RunningArachne(pages, data)
        try:
            with self.assertRaisesRegex(AssertionError, "cannot load persisted ruling"):
                service.start()
        finally:
            service.stop()

    def test_ruling_validation_fails_loud(self) -> None:
        request = Request(
            f"{self.service.url}/ruling",
            data=b"{}",
            headers={"Content-Type": "application/json", **bearer(self.service.token)},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        problem = json.loads(raised.exception.read())
        self.assertEqual(problem["error"], "invalid_ruling")
        self.assertTrue(problem["detail"])

    def test_publisher_enforces_v2_nav_contract(self) -> None:
        source = Path(self.temporary.name) / "decision_publish.html"
        destination_dir = Path(self.temporary.name) / "published"
        source.write_text(
            '<!doctype html><html data-issue="publish"><main>'
            "<h1>Argument</h1><p>Compare paths.</p></main></html>",
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
        self.assertEqual(published, source.read_text(encoding="utf-8"))
        sidecar = json.loads(
            (destination_dir / ".meta" / f"{source.name}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(sidecar, {"issue": "publish"})

        legacy = Path(self.temporary.name) / "decision_legacy.html"
        legacy.write_text(
            '<html data-issue="legacy"><script>'
            "fetch('http://127.0.0.1:8788/ruling'); "
            "localStorage.setItem('draft', 'yes')</script></html>",
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                "--pages-dir",
                str(destination_dir),
                str(legacy),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("forbidden /ruling", rejected.stderr)
        self.assertFalse((destination_dir / legacy.name).exists())

    def test_publisher_handles_multiple_brief_owned_issues(self) -> None:
        destination_dir = Path(self.temporary.name) / "published-many"
        first = Path(self.temporary.name) / "decision_alpha.html"
        second = Path(self.temporary.name) / "decision_beta.html"
        first.write_text(
            '<!doctype html><html data-issue="alpha"><main>Alpha</main></html>',
            encoding="utf-8",
        )
        second.write_text(
            '<!doctype html><body data-issue="beta"><main>Beta</main></body>',
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                str(first),
                str(second),
                "--pages-dir",
                str(destination_dir),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("published:"), 2)
        for source, issue in ((first, "alpha"), (second, "beta")):
            self.assertEqual(
                (destination_dir / source.name).read_text(encoding="utf-8"),
                source.read_text(encoding="utf-8"),
            )
            sidecar = json.loads(
                (
                    destination_dir / ".meta" / f"{source.name}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(sidecar, {"issue": issue})

        rejected = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                str(first),
                str(second),
                "--pages-dir",
                str(destination_dir),
                "--issue",
                "override",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("--issue is valid only with a single source", rejected.stderr)

        missing = Path(self.temporary.name) / "decision_missing.html"
        missing.write_text(
            "<!doctype html><html><main>No issue</main></html>",
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                str(missing),
                "--pages-dir",
                str(destination_dir),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("no data-issue", rejected.stderr)
        self.assertFalse((destination_dir / missing.name).exists())

        override = Path(self.temporary.name) / "decision_override.html"
        override.write_text(
            "<!doctype html><html><main>Explicit issue</main></html>",
            encoding="utf-8",
        )
        accepted = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "publish-page.py"),
                str(override),
                "--pages-dir",
                str(destination_dir),
                "--issue",
                "explicit",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(
                (
                    destination_dir / ".meta" / f"{override.name}.json"
                ).read_text(encoding="utf-8")
            ),
            {"issue": "explicit"},
        )

    def test_bootstrap_url_helper_uses_fragment_for_the_secret(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "bootstrap-url.py"),
                "--base-url",
                "https://arachne.example.test",
                "--token-file",
                str(self.service.token_file),
                "decision_476.html",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = urlsplit(result.stdout.strip())
        self.assertEqual(parsed.path, "/bootstrap")
        self.assertEqual(parse_qs(parsed.query), {"next": ["decision_476.html"]})
        self.assertEqual(parse_qs(parsed.fragment), {"token": [self.service.token]})
        self.assertNotIn(self.service.token, parsed.query)


if __name__ == "__main__":
    unittest.main(verbosity=2)
