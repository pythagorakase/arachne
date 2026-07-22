from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from page_contract import prepare_html
from semantic_snapshot import build_snapshot, validate_llm_alternatives
from share_server import PUBLIC_SHARE_CSP, SHARE_LOG_ID, Config, PublicShareServer
from share_store import SHARE_TTL_SECONDS, ShareStore


REPO = Path(__file__).resolve().parents[1]


SHAREABLE_PAGE = """<!doctype html>
<html lang="en"><head>
<title>Choose the bridge</title>
<style>body { color: red }</style>
</head><body><main>
<h1>Which bridge should carry the traffic?</h1>
<p>Constraint: retain the <strong>existing route</strong> during migration.</p>
<figure data-arachne-visual>
  <svg role="img"><script>window.bad = true</script></svg>
  <template data-arachne-llm-alt>
    <h2>Traffic comparison</h2>
    <p>Bridge A carries 60 requests/second; Bridge B carries 90.</p>
    <ul><li>At peak, A saturates.</li><li>B retains 25% headroom.</li></ul>
  </template>
  <figcaption>Peak capacity comparison.</figcaption>
</figure>
<table><tr><th>Bridge</th><th>Latency</th></tr>
<tr><td>A</td><td>80 ms</td></tr><tr><td>B</td><td>52 ms</td></tr></table>
<form><fieldset><legend>Selection</legend>
  <label><input type="radio" name="bridge" value="a"><b>A</b>
    <span>Smaller change, but no peak headroom.</span></label>
  <label><input type="radio" name="bridge" value="b"><b>B</b>
    <span>Larger change with adequate headroom.</span></label>
  <textarea name="notes" placeholder="Optional reasoning"></textarea>
  <textarea readonly>Existing route must remain available.</textarea>
  <button type="submit" onclick="steal()">Send</button>
</fieldset></form>
<template><p>dormant implementation text</p></template>
<script>fetch('/rulings?since=0')</script>
</main></body></html>"""


class SemanticSnapshotTests(unittest.TestCase):
    def test_contract_requires_llm_text_for_substantive_visuals(self) -> None:
        missing = """<!doctype html><main>
        <figure><svg role="img"></svg><figcaption>A chart.</figcaption></figure>
        </main>"""
        with self.assertRaisesRegex(ValueError, "data-arachne-llm-alt"):
            validate_llm_alternatives(missing)
        with self.assertRaisesRegex(ValueError, "data-arachne-llm-alt"):
            prepare_html("decision_missing_alt.html", missing)

        decorative = """<!doctype html><main>
        <svg aria-hidden="true"><path></path></svg>
        <figure data-arachne-decorative><canvas></canvas></figure>
        </main>"""
        self.assertEqual(
            prepare_html("decision_decorative.html", decorative), decorative
        )

        ordinary_image = '<main><img src="ignored.png" alt="A coral thread"></main>'
        validate_llm_alternatives(ordinary_image)

        pictured = """<main><figure><img src="ignored.png"
        alt="Two routes converge at the same bridge"></figure></main>"""
        validate_llm_alternatives(pictured)

        attribute_alt = """<main><figure data-arachne-llm-alt="A rises above B">
        <svg role="img"><path></path></svg></figure></main>"""
        validate_llm_alternatives(attribute_alt)

    def test_llm_alt_must_be_inert_and_nonempty(self) -> None:
        for source in (
            '<figure><template data-arachne-llm-alt> </template></figure>',
            """<figure><template data-arachne-llm-alt>
            <script>not inert</script><p>Description</p></template></figure>""",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_llm_alternatives(source)

    def test_html_and_markdown_share_one_complete_inert_semantic_source(self) -> None:
        snapshot = build_snapshot(
            SHAREABLE_PAGE,
            issue="bridge-42",
            created_at="2026-07-22T12:00:00.000Z",
            expires_at="2026-08-21T12:00:00.000Z",
        )

        shared_facts = (
            "Which bridge should carry the traffic?",
            "existing route",
            "Bridge A carries 60 requests/second",
            "B retains 25% headroom",
            "A saturates",
            "Peak capacity comparison",
            "80 ms",
            "52 ms",
            "Smaller change, but no peak headroom",
            "Larger change with adequate headroom",
            "Optional reasoning",
            "Existing route must remain available",
        )
        for fact in shared_facts:
            with self.subTest(fact=fact):
                self.assertIn(fact, snapshot.html)
                self.assertIn(fact, snapshot.markdown)
        for forbidden in (
            "<script",
            "<style>body",
            "<form",
            "<input",
            "<textarea",
            "<button",
            "onclick",
            "fetch('/rulings",
        ):
            self.assertNotIn(forbidden, snapshot.html)
        self.assertNotIn("window.bad", snapshot.html)
        self.assertNotIn("dormant implementation text", snapshot.html)
        self.assertNotIn("dormant implementation text", snapshot.markdown)
        self.assertIn("Visual text equivalent", snapshot.html)
        self.assertIn("Visual text equivalent", snapshot.markdown)
        self.assertIn("Choose one:", snapshot.html)
        self.assertIn("Choose one:", snapshot.markdown)
        self.assertRegex(snapshot.canonical_sha256, r"\A[0-9a-f]{64}\Z")
        self.assertIn(snapshot.canonical_sha256, snapshot.html)
        self.assertIn(snapshot.canonical_sha256, snapshot.markdown)


class ShareStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = [1_750_000_000.0]
        self.store = ShareStore(
            Path(self.temporary.name) / "shares", clock=lambda: self.now[0]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_reuse_change_revoke_and_expiry(self) -> None:
        first = self.store.create_or_reuse(
            page="decision_bridge.html", issue="bridge-42", source=SHAREABLE_PAGE
        )
        self.assertFalse(first.reused)
        self.assertEqual(len(first.share.share_id), 32)
        directory = self.store.root / first.share.share_id
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for filename in ("metadata.json", "snapshot.html", "snapshot.md"):
            self.assertEqual((directory / filename).stat().st_mode & 0o777, 0o600)

        metadata = json.loads((directory / "metadata.json").read_text())
        self.assertEqual(
            metadata["expires_epoch"] - self.now[0], SHARE_TTL_SECONDS
        )
        self.assertIsNone(metadata["revoked_at"])
        self.assertIsNotNone(self.store.read(first.share.share_id, "html"))
        self.assertIsNotNone(self.store.read(first.share.share_id, "markdown"))

        reused = self.store.create_or_reuse(
            page="decision_bridge.html", issue="bridge-42", source=SHAREABLE_PAGE
        )
        self.assertTrue(reused.reused)
        self.assertEqual(reused.share.share_id, first.share.share_id)

        changed = self.store.create_or_reuse(
            page="decision_bridge.html",
            issue="bridge-42",
            source=SHAREABLE_PAGE.replace("90", "95"),
        )
        self.assertFalse(changed.reused)
        self.assertNotEqual(changed.share.share_id, first.share.share_id)

        retitled = self.store.create_or_reuse(
            page="decision_bridge.html",
            issue="bridge-42",
            source=SHAREABLE_PAGE.replace(
                "<title>Choose the bridge</title>",
                "<title>Choose the durable bridge</title>",
            ),
        )
        self.assertFalse(retitled.reused)
        self.assertNotEqual(retitled.share.share_id, first.share.share_id)

        self.assertTrue(self.store.revoke(first.share.share_id))
        self.assertFalse(self.store.revoke(first.share.share_id))
        self.assertIsNone(self.store.read(first.share.share_id, "html"))
        self.assertTrue((directory / "metadata.json").is_file())

        self.now[0] += SHARE_TTL_SECONDS
        self.assertIsNone(self.store.read(changed.share.share_id, "markdown"))

    def test_unknown_malformed_and_corrupt_shares_are_unavailable(self) -> None:
        self.assertIsNone(self.store.get("A" * 32))
        self.assertIsNone(self.store.read("../metadata", "html"))
        with self.assertRaises(ValueError):
            self.store.read("A" * 32, "pdf")

        created = self.store.create_or_reuse(
            page="decision_bridge.html", issue="bridge-42", source=SHAREABLE_PAGE
        ).share
        path = self.store.root / created.share_id / "snapshot.html"
        path.write_text("tampered", encoding="utf-8")
        self.assertIsNone(self.store.read(created.share_id, "html"))

        metadata_share = self.store.create_or_reuse(
            page="decision_metadata.html",
            issue="bridge-43",
            source=SHAREABLE_PAGE,
        ).share
        metadata_path = self.store.root / metadata_share.share_id / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["expires_at"] = metadata["created_at"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertIsNone(self.store.get(metadata_share.share_id))

    def test_share_root_must_not_be_a_symlink(self) -> None:
        target = Path(self.temporary.name) / "real-shares"
        target.mkdir()
        link = Path(self.temporary.name) / "linked-shares"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            ShareStore(link)


class PublicShareHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "shares"
        self.store = ShareStore(root)
        self.share = self.store.create_or_reuse(
            page="decision_bridge.html", issue="bridge-42", source=SHAREABLE_PAGE
        ).share
        self.server = PublicShareServer(Config(share_dir=root, port=0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_only_live_html_and_markdown_capabilities_are_public(self) -> None:
        rendered_log = SHARE_LOG_ID.sub(
            r"\1<redacted>", f'GET /s/{self.share.share_id}.md HTTP/1.1'
        )
        self.assertNotIn(self.share.share_id, rendered_log)

        with urlopen(f"{self.url}/s/{self.share.share_id}", timeout=2) as response:
            html_body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(response.headers["Content-Security-Policy"], PUBLIC_SHARE_CSP)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Server"], "ArachneShare/1")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertIn("Which bridge", html_body)

        markdown_url = f"{self.url}/s/{self.share.share_id}.md"
        with urlopen(markdown_url, timeout=2) as response:
            markdown = response.read().decode()
            self.assertEqual(
                response.headers["Content-Type"], "text/markdown; charset=utf-8"
            )
        self.assertIn("Bridge A carries 60 requests/second", markdown)

        request = Request(markdown_url, method="HEAD")
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertGreater(int(response.headers["Content-Length"]), 100)

        for path in (
            "/",
            "/shares",
            "/rulings?since=0",
            f"/s/{self.share.share_id}?download=1",
            "/s/%2e%2e/metadata.json",
            "/s/" + "B" * 32,
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                urlopen(f"{self.url}{path}", timeout=2)
            self.assertEqual(raised.exception.code, 404)

        for verb in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(verb=verb), self.assertRaises(HTTPError) as method:
                urlopen(Request(markdown_url, data=b"", method=verb), timeout=2)
            self.assertEqual(method.exception.code, 405)
            self.assertEqual(method.exception.headers["Allow"], "GET, HEAD")
            self.assertEqual(
                method.exception.headers["Content-Security-Policy"],
                PUBLIC_SHARE_CSP,
            )

        self.assertTrue(self.store.revoke(self.share.share_id))
        for suffix in ("", ".md"):
            with self.assertRaises(HTTPError) as revoked:
                urlopen(f"{self.url}/s/{self.share.share_id}{suffix}", timeout=2)
            self.assertEqual(revoked.exception.code, 404)


class DeploymentBoundaryTests(unittest.TestCase):
    def test_public_tunnel_can_only_reach_the_share_listener(self) -> None:
        config = (REPO / "deploy/cloudflared/share-config.yml.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("service: http://127.0.0.1:8791", config)
        self.assertIn("- service: http_status:404", config)
        self.assertNotIn("8788", config)
        self.assertNotIn("8790", config)

        unit = (REPO / "deploy/systemd/arachne-share.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("run-configured-service.sh share", unit)
        self.assertNotIn("server.py", unit)


if __name__ == "__main__":
    unittest.main()
