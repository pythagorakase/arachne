from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import (
    fallback_title,
    page_title,
    render_bootstrap,
    render_inbox,
    render_locked_inbox,
)


REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"


class UiStructureTests(unittest.TestCase):
    def test_application_browser_surfaces_live_under_ui(self) -> None:
        expected_assets = {
            "README.md",
            "__init__.py",
            "bootstrap.html",
            "brief.html",
            "empty.html",
            "inbox-content.html",
            "inbox.css",
            "inbox.html",
            "locked.html",
            "render.py",
        }
        actual_assets = {path.name for path in UI.iterdir() if path.is_file()}
        self.assertTrue(expected_assets <= actual_assets)
        server_source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("<!doctype html>", server_source)
        self.assertNotIn("class=\"brief\"", server_source)
        self.assertNotIn("color-scheme: dark", server_source)

    def test_inbox_rendering_fills_assets_and_escapes_page_copy(self) -> None:
        pending = [
            {
                "name": "decision_476.html",
                "issue": "476",
                "title": "A <thorny> & important choice",
                "published_at": 0.0,
            }
        ]
        archived = [
            {
                "name": "decision_480.html",
                "issue": "480",
                "title": "Past decision",
                "published_at": 0.0,
                "ruled_at": 60.0,
                "ruling_sequence": 7,
            }
        ]

        rendered = render_inbox(pending, archived).decode("utf-8")

        self.assertIn("Awaiting ruling · 1", rendered)
        self.assertIn("Archive · 1", rendered)
        self.assertIn("A &lt;thorny&gt; &amp; important choice", rendered)
        self.assertNotIn("A <thorny>", rendered)
        self.assertIn("ruling 7", rendered)
        self.assertIn(":root { color-scheme: dark; }", rendered)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_locked_inbox_uses_the_same_shell_without_disclosing_briefs(self) -> None:
        rendered = render_locked_inbox().decode("utf-8")

        self.assertIn("no live Arachne session", rendered)
        self.assertIn("<header><h1>Arachne</h1>", rendered)
        self.assertNotIn("Awaiting ruling", rendered)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_bootstrap_rendering_binds_and_redirects_to_the_requested_page(self) -> None:
        rendered = render_bootstrap(
            "decision_476.html", "/decision_476.html"
        ).decode("utf-8")

        self.assertIn('{ticket, page: "decision_476.html"}', rendered)
        self.assertIn('location.replace("/decision_476.html")', rendered)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_page_titles_and_fallbacks_remain_presentation_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            decision = Path(directory) / "decision_476_relationship_drift.html"
            decision.write_text(
                "<!doctype html><title>  Rock &amp;\n  Roll  </title>",
                encoding="utf-8",
            )
            self.assertEqual(page_title(decision), "Rock & Roll")

        self.assertEqual(
            fallback_title("decision_476_relationship_drift.html", "476"),
            "relationship drift",
        )

    def test_slot_validator_catches_digit_bearing_markers(self) -> None:
        # A stray slot the renderer cannot fill must fail loud even when its
        # name carries a digit (future docket slots like AXIS_2 / PANE_1).
        from ui import render

        with self.assertRaises(RuntimeError):
            render._fill_template(
                "probe",
                "<p>@@ARACHNE_OK@@ @@ARACHNE_EXTRA_2@@</p>",
                {"@@ARACHNE_OK@@": "x"},
            )


if __name__ == "__main__":
    unittest.main()
