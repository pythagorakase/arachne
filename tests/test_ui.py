from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from page_contract import prepare_html

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
            "brief-scroll-sync.js",
            "brief.html",
            "empty.html",
            "inbox-content.html",
            "inbox.css",
            "inbox.html",
            "inbox.js",
            "locked.html",
            "render.py",
        }
        actual_assets = {path.name for path in UI.iterdir() if path.is_file()}
        self.assertTrue(expected_assets <= actual_assets)
        self.assertEqual(
            {path.name for path in (UI / "fonts").glob("*.ttf")},
            {
                "Cinzel-wght.ttf",
                "Megrim.ttf",
                "Spectral-Regular.ttf",
                "Spectral-SemiBold.ttf",
            },
        )
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
                "repo": "NEXUS · Orrery",
                "axis_count": 2,
                "published_at": 0.0,
            }
        ]
        archived = [
            {
                "name": "decision_480.html",
                "issue": "480",
                "title": "Past decision",
                "repo": "NEXUS · Archive",
                "axis_count": 1,
                "published_at": 0.0,
                "ruled_at": 60.0,
                "ruling_sequence": 7,
            }
        ]

        rendered = render_inbox(pending, archived).decode("utf-8")

        self.assertIn('data-list-count="awaiting">1</span>', rendered)
        self.assertIn('data-list-count="archive">1</span>', rendered)
        self.assertIn('class="app-frame" data-arachne-shell', rendered)
        self.assertIn('class="list-pane"', rendered)
        self.assertIn('class="reading-pane"', rendered)
        self.assertIn('class="docket-pane"', rendered)
        self.assertIn('class="brief-frame"', rendered)
        frame_start = rendered.index("<iframe")
        frame_tag = rendered[frame_start : rendered.index(">", frame_start) + 1]
        self.assertIn('data-reading-frame', frame_tag)
        self.assertIn('sandbox="allow-scripts"', frame_tag)
        self.assertNotIn("allow-same-origin", frame_tag)
        self.assertNotIn("allow-forms", frame_tag)
        self.assertIn("data-axis-list", rendered)
        self.assertIn("/axes/", rendered)
        self.assertIn("NEXUS · Orrery", rendered)
        self.assertIn('data-axis-count="2"', rendered)
        self.assertIn("A &lt;thorny&gt; &amp; important choice", rendered)
        self.assertNotIn("A <thorny>", rendered)
        self.assertIn("ruling 7", rendered)
        self.assertIn("color-scheme: dark;", rendered)
        self.assertIn("localStorage", rendered)
        self.assertIn('fetch("/ruling"', rendered)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_phone_shell_ribbon_and_scroll_sync_assets_are_complete(self) -> None:
        css = (UI / "inbox.css").read_text(encoding="utf-8")
        client = (UI / "inbox.js").read_text(encoding="utf-8")
        rendered = render_inbox([], []).decode("utf-8")

        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".app-frame.is-phone-reading .ruling-ribbon", css)
        self.assertIn(".ribbon-axis-dot.is-active", css)
        for hook in (
            "data-phone-inbox",
            "data-phone-reading-context",
            "data-ruling-ribbon",
            "data-ribbon-axis-stepper",
            "data-ribbon-axis-controls",
            "data-ribbon-note",
            "data-ribbon-progress",
            "data-ribbon-send",
        ):
            self.assertIn(hook, rendered)
        self.assertIn("event.source !== frame.contentWindow", client)
        self.assertIn('source: CHROME_MESSAGE_SOURCE, type: "scroll-to"', client)
        self.assertIn(
            'source: CHROME_MESSAGE_SOURCE, type: "request-in-view"', client
        )
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_scroll_sync_fixture_embeds_the_canonical_reporter(self) -> None:
        canonical = (UI / "brief-scroll-sync.js").read_text(encoding="utf-8").strip()
        fixture_path = REPO / "examples" / "docket-scroll-sync-test.html"
        fixture = fixture_path.read_text(encoding="utf-8")
        marker = '<script data-arachne-brief-scroll-sync>'
        embedded = fixture.split(marker, 1)[1].split("</script>", 1)[0].strip()
        manifest = json.loads(
            (REPO / "examples" / "docket-scroll-sync-test.axes.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(embedded, canonical)
        for axis_id in ("scope", "evolution", "custody"):
            self.assertIn(f'data-axis="{axis_id}"', fixture)
        self.assertNotIn("<script src=", fixture)
        self.assertEqual(
            prepare_html(fixture_path.name, fixture, manifest),
            (fixture, manifest),
        )

    def test_locked_inbox_uses_the_same_shell_without_disclosing_briefs(self) -> None:
        rendered = render_locked_inbox().decode("utf-8")

        self.assertIn("no live Arachne session", rendered)
        self.assertIn("<header><h1>Arachne</h1>", rendered)
        self.assertNotIn("Awaiting ruling", rendered)
        self.assertNotIn("data-arachne-shell", rendered)
        self.assertNotIn("decision_476.html", rendered)
        self.assertNotIn("/axes/", rendered)
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


@unittest.skipUnless(shutil.which("node"), "node is required for inbox client tests")
class InboxClientJavaScriptTests(unittest.TestCase):
    def run_node(self, source: str) -> None:
        result = subprocess.run(
            ["node", "-e", source],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stale_discuss_note_is_omitted_after_switch_to_plain_choice(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {composeRulingPayload} = require("./ui/inbox.js");
const manifest = {
  issue: "stale-note",
  overall_notes: false,
  axes: [{
    id: "scope",
    label: "Scope",
    select: "one",
    notes: false,
    options: [{id: "focused", label: "Focused"}],
  }],
};
const draft = {
  issue: "stale-note",
  overall: "",
  axes: {
    scope: {
      mode: "choice",
      choice: "focused",
      note: "stale discussion context",
    },
  },
};
const payload = composeRulingPayload(manifest, draft);
assert.equal(payload.issue, "stale-note");
assert.equal(payload.form.scope, "focused");
assert.equal(
  Object.prototype.hasOwnProperty.call(payload.form, "scope-notes"),
  false,
);
assert.equal(payload.markdown, "Scope: Focused");
"""
        )

    def test_brief_in_view_message_validation_is_exact_and_axis_scoped(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {isValidBriefInViewMessage} = require("./ui/inbox.js");
const knownAxes = new Set(["scope", "evolution"]);
const valid = {source: "arachne-brief", type: "in-view", axis: "scope"};

assert.equal(isValidBriefInViewMessage(valid, knownAxes), true);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-chrome", type: "in-view", axis: "scope"},
  knownAxes,
), false);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-brief", type: "scroll-to", axis: "scope"},
  knownAxes,
), false);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-brief", type: "in-view", axis: "unknown"},
  knownAxes,
), false);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-brief", type: "in-view", axis: 1},
  knownAxes,
), false);
assert.equal(isValidBriefInViewMessage({...valid, extra: true}, knownAxes), false);
assert.equal(isValidBriefInViewMessage(null, knownAxes), false);
assert.equal(isValidBriefInViewMessage([], knownAxes), false);
assert.equal(isValidBriefInViewMessage({}, knownAxes), false);
"""
        )

    def test_pending_chrome_scroll_accepts_only_its_target_report(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {shouldAcceptInViewReport} = require("./ui/inbox.js");

assert.equal(shouldAcceptInViewReport(null, "scope"), true);
assert.equal(shouldAcceptInViewReport("custody", "scope"), false);
assert.equal(shouldAcceptInViewReport("custody", "evolution"), false);
assert.equal(shouldAcceptInViewReport("custody", "custody"), true);
"""
        )

    def test_response_failures_distinguish_rejection_from_uncertainty(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {
  readRulingAcknowledgement,
  submissionFailureKind,
} = require("./ui/inbox.js");

async function captured(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("expected the acknowledgement to be rejected");
}

(async () => {
  const rejected = await captured(readRulingAcknowledgement(
    new Response(JSON.stringify({detail: "axis is incomplete"}), {
      status: 400,
      headers: {"Content-Type": "application/json"},
    }),
    "476",
  ));
  assert.equal(submissionFailureKind(rejected), "definitely-not-filed");
  assert.equal(rejected.message, "axis is incomplete");

  const garbled = await captured(readRulingAcknowledgement(
    new Response("not-json", {status: 201}),
    "476",
  ));
  assert.equal(submissionFailureKind(garbled), "ambiguous");

  const mismatched = await captured(readRulingAcknowledgement(
    new Response(JSON.stringify({issue: "477", sequence: 1}), {status: 201}),
    "476",
  ));
  assert.equal(submissionFailureKind(mismatched), "ambiguous");

  const unreadableRejection = await captured(readRulingAcknowledgement(
    new Response("not-json", {status: 500}),
    "476",
  ));
  assert.equal(submissionFailureKind(unreadableRejection), "ambiguous");
  assert.equal(
    submissionFailureKind(new TypeError("network connection failed")),
    "ambiguous",
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        )


if __name__ == "__main__":
    unittest.main()
