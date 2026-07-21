from __future__ import annotations

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
            "brief-agent.js",
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
        self.assertNotIn("brief-scroll-sync.js", actual_assets)
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
        self.assertNotIn('class="brief"', server_source)
        self.assertNotIn("color-scheme: dark", server_source)

    def test_inbox_rendering_fills_nav_assets_and_escapes_page_copy(self) -> None:
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

        self.assertIn('data-list-count="awaiting">1</span>', rendered)
        self.assertIn('data-list-count="archive">1</span>', rendered)
        self.assertIn('class="app-frame" data-arachne-shell', rendered)
        self.assertIn('class="list-pane"', rendered)
        self.assertIn('class="reading-pane"', rendered)
        self.assertIn('<nav class="ruling-nav"', rendered)
        self.assertIn('class="brief-frame"', rendered)
        frame_start = rendered.index("<iframe")
        frame_tag = rendered[frame_start : rendered.index(">", frame_start) + 1]
        self.assertIn("data-reading-frame", frame_tag)
        self.assertIn('sandbox="allow-scripts"', frame_tag)
        self.assertNotIn("allow-same-origin", frame_tag)
        self.assertNotIn("allow-forms", frame_tag)
        self.assertIn("data-part-outline", rendered)
        self.assertIn("data-nav-decision-title", rendered)
        self.assertIn('data-part-count="0"', rendered)
        self.assertNotIn("/axes/", rendered)
        self.assertIn("A &lt;thorny&gt; &amp; important choice", rendered)
        self.assertNotIn("A <thorny>", rendered)
        self.assertIn("ruling 7", rendered)
        self.assertIn("color-scheme: dark;", rendered)
        self.assertIn("arachne:draft:v3:", rendered)
        self.assertIn('fetch("/ruling"', rendered)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_phone_shell_is_a_compact_part_nav_without_capture_controls(self) -> None:
        css = (UI / "inbox.css").read_text(encoding="utf-8")
        client = (UI / "inbox.js").read_text(encoding="utf-8")
        rendered = render_inbox([], []).decode("utf-8")

        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".app-frame.is-phone-reading .ruling-ribbon", css)
        self.assertIn(".ribbon-part-dot.is-active", css)
        self.assertIn(".ribbon-part-dot.is-answered", css)
        for hook in (
            "data-phone-inbox",
            "data-phone-reading-context",
            "data-ruling-ribbon",
            "data-ribbon-part-stepper",
            "data-ribbon-progress",
            "data-ribbon-send",
        ):
            self.assertIn(hook, rendered)
        for removed in (
            "data-ribbon-axis-controls",
            "data-ribbon-note",
            "ribbon-option-pill",
        ):
            self.assertNotIn(removed, rendered)
        self.assertIn("event.source !== frame.contentWindow", client)
        self.assertIn('source: CHROME_MESSAGE_SOURCE, type: "scroll-to"', client)
        self.assertIn(
            'source: CHROME_MESSAGE_SOURCE, type: "request-in-view"', client
        )
        self.assertIn('source: CHROME_MESSAGE_SOURCE, type: "restore", form', client)
        self.assertIn('type: "collect", token', client)
        self.assertIn("state.expectingChromeLoad = true", client)
        self.assertIn("invalidateForeignFrameDocument", client)
        self.assertNotIn("@@ARACHNE_", rendered)

    def test_nav_capture_fixture_embeds_the_canonical_agent(self) -> None:
        canonical = (UI / "brief-agent.js").read_text(encoding="utf-8").strip()
        fixture_path = REPO / "examples" / "nav-capture-test.html"
        fixture = fixture_path.read_text(encoding="utf-8")
        marker = '<script data-arachne-brief-agent>'
        embedded = fixture.split(marker, 1)[1].split("</script>", 1)[0].strip()

        self.assertEqual(embedded, canonical)
        for part_id in ("scope", "channels", "rationale"):
            self.assertIn(f'data-decision="{part_id}"', fixture)
        self.assertGreaterEqual(fixture.count('name="channels"'), 2)
        self.assertIn('type="radio" name="scope"', fixture)
        self.assertIn('textarea id="rationale-text" name="rationale"', fixture)
        self.assertIn('data-issue="nav-capture-test"', fixture)
        self.assertNotIn("<script src=", fixture)
        self.assertNotIn("/ruling", fixture)
        self.assertNotIn("localStorage", fixture)
        self.assertEqual(prepare_html(fixture_path.name, fixture), fixture)
        self.assertFalse(
            (REPO / "examples" / "nav-capture-test.axes.json").exists()
        )

    def test_locked_inbox_discloses_neither_briefs_nor_client_script(self) -> None:
        rendered = render_locked_inbox().decode("utf-8")

        self.assertIn("no live Arachne session", rendered)
        self.assertIn("<header><h1>Arachne</h1>", rendered)
        self.assertNotIn("Awaiting ruling", rendered)
        self.assertNotIn("data-arachne-shell", rendered)
        self.assertNotIn("decision_476.html", rendered)
        self.assertNotIn("arachne:draft:v3:", rendered)
        self.assertNotIn('fetch("/ruling"', rendered)
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

    def test_brief_agent_serializes_single_and_multi_value_controls(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {serializeForm} = require("./ui/brief-agent.js");
const controls = [
  {tagName: "INPUT", type: "radio", name: "scope", value: "focused", checked: false},
  {tagName: "INPUT", type: "radio", name: "scope", value: "broad", checked: true},
  {tagName: "INPUT", type: "checkbox", name: "channels", value: "email", checked: true},
  {tagName: "INPUT", type: "checkbox", name: "channels", value: "slack", checked: false},
  {tagName: "INPUT", type: "checkbox", name: "channels", value: "phone", checked: true},
  {tagName: "TEXTAREA", name: "rationale", value: "Ship the narrow path."},
  {
    tagName: "SELECT",
    name: "reviewers",
    multiple: true,
    selectedOptions: [
      {value: "design", selected: true},
      {value: "security", selected: true},
    ],
  },
];

assert.deepEqual(serializeForm(controls), {
  scope: "broad",
  channels: ["email", "phone"],
  rationale: "Ship the narrow path.",
  reviewers: ["design", "security"],
});
"""
        )

    def test_brief_agent_default_markdown_preserves_part_order(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {composeMarkdown} = require("./ui/brief-agent.js");
const form = {
  scope: "broad",
  channels: ["email", "phone"],
  rationale: "Because it is reversible.",
  empty: "",
};
const parts = [
  {id: "scope", label: "Scope", names: ["scope"]},
  {id: "channels", label: "Channels", names: ["channels"]},
  {id: "rationale", label: "Rationale", names: ["rationale"]},
  {id: "followup", label: "Follow-up", names: ["empty"]},
  {id: "ack", label: "Acknowledgement", names: [], answered: true},
];

assert.equal(
  composeMarkdown(form, parts),
  [
    "Scope: broad",
    "Channels: email, phone",
    "Rationale: Because it is reversible.",
    "Follow-up: — (unanswered)",
    "Acknowledgement: (no value)",
  ].join("\n"),
);
"""
        )

    def test_brief_agent_parent_message_validation_uses_exact_shapes(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {isValidParentMessage} = require("./ui/brief-agent.js");

assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "request-in-view"},
), true);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "scroll-to", axis: "scope"},
), true);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "restore", form: {scope: "broad"}},
), true);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "collect", token: "send-1"},
), true);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "collect", token: "send-1", extra: true},
), false);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "collect", token: 1},
), false);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "restore", form: [], extra: true},
), false);
assert.equal(isValidParentMessage(
  {source: "arachne-brief", type: "scroll-to", axis: "scope"},
), false);
assert.equal(isValidParentMessage(
  {source: "arachne-chrome", type: "request-in-view", extra: true},
), false);
assert.equal(isValidParentMessage(null), false);
"""
        )

    def test_brief_agent_restores_generic_control_shapes(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {restoreForm} = require("./ui/brief-agent.js");
const options = [
  {value: "design", selected: false},
  {value: "security", selected: false},
];
const controls = [
  {tagName: "INPUT", type: "radio", name: "scope", value: "focused", checked: true},
  {tagName: "INPUT", type: "radio", name: "scope", value: "broad", checked: false},
  {tagName: "INPUT", type: "checkbox", name: "channels", value: "email", checked: false},
  {tagName: "INPUT", type: "checkbox", name: "channels", value: "phone", checked: false},
  {tagName: "TEXTAREA", name: "rationale", value: ""},
  {tagName: "SELECT", name: "reviewers", multiple: true, options},
  {tagName: "TEXTAREA", name: "authored", value: "Keep this default."},
  {tagName: "INPUT", type: "checkbox", name: "default-on", value: "yes", checked: true},
];

restoreForm(controls, {
  scope: "broad",
  channels: ["email", "phone"],
  rationale: "Restore this explanation.",
  reviewers: ["security"],
});
assert.equal(controls[0].checked, false);
assert.equal(controls[1].checked, true);
assert.equal(controls[2].checked, true);
assert.equal(controls[3].checked, true);
assert.equal(controls[4].value, "Restore this explanation.");
assert.deepEqual(options.map((option) => option.selected), [false, true]);
assert.equal(controls[6].value, "Keep this default.");
assert.equal(controls[7].checked, true);
"""
        )

    def test_collect_and_ruling_messages_have_exact_correlated_shapes(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {
  isValidParentMessage,
  makeRulingMessage,
} = require("./ui/brief-agent.js");
const {
  isValidBriefRulingMessage,
  makeCollectMessage,
  rulingMatchesPendingToken,
} = require("./ui/inbox.js");

const collect = makeCollectMessage("fresh-send-7");
assert.deepEqual(collect, {
  source: "arachne-chrome",
  type: "collect",
  token: "fresh-send-7",
});
assert.equal(isValidParentMessage(collect), true);
assert.throws(() => makeCollectMessage(""), /non-empty string/);

const ruling = makeRulingMessage(
  "fresh-send-7",
  {scope: "broad"},
  "Scope: broad",
  true,
);
assert.deepEqual(ruling, {
  source: "arachne-brief",
  type: "ruling",
  token: "fresh-send-7",
  form: {scope: "broad"},
  markdown: "Scope: broad",
  allAnswered: true,
});
assert.equal(isValidBriefRulingMessage(ruling), true);
assert.equal(isValidBriefRulingMessage({...ruling, extra: true}), false);
assert.equal(isValidBriefRulingMessage({...ruling, token: 7}), false);
assert.equal(isValidBriefRulingMessage({...ruling, form: []}), false);
assert.equal(isValidBriefRulingMessage({...ruling, source: "arachne-chrome"}), false);
assert.equal(rulingMatchesPendingToken(ruling, "fresh-send-7"), true);
assert.equal(rulingMatchesPendingToken(ruling, "another-send"), false);
"""
        )

    def test_chrome_capture_message_validation_is_exact_and_consistent(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {isValidBriefCaptureMessage} = require("./ui/inbox.js");
const valid = {
  source: "arachne-brief",
  type: "capture",
  issue: "476",
  parts: [
    {id: "scope", label: "Scope", answered: true},
    {id: "channels", label: "Channels", answered: true},
  ],
  allAnswered: true,
  form: {scope: "broad", channels: ["email", "phone"]},
  markdown: "Scope: broad\nChannels: email, phone",
};

assert.equal(isValidBriefCaptureMessage(valid), true);
assert.equal(isValidBriefCaptureMessage({...valid, extra: true}), false);
assert.equal(isValidBriefCaptureMessage({...valid, source: "arachne-chrome"}), false);
assert.equal(isValidBriefCaptureMessage({...valid, form: []}), false);
assert.equal(isValidBriefCaptureMessage({...valid, allAnswered: false}), false);
assert.equal(isValidBriefCaptureMessage({
  ...valid,
  parts: [...valid.parts, {...valid.parts[0]}],
}), false);
assert.equal(isValidBriefCaptureMessage(null), false);
"""
        )

    def test_brief_in_view_message_validation_is_exact_and_part_scoped(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {isValidBriefInViewMessage} = require("./ui/inbox.js");
const knownParts = new Set(["scope", "channels"]);
const valid = {source: "arachne-brief", type: "in-view", axis: "scope"};

assert.equal(isValidBriefInViewMessage(valid, knownParts), true);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-chrome", type: "in-view", axis: "scope"},
  knownParts,
), false);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-brief", type: "scroll-to", axis: "scope"},
  knownParts,
), false);
assert.equal(isValidBriefInViewMessage(
  {source: "arachne-brief", type: "in-view", axis: "unknown"},
  knownParts,
), false);
assert.equal(isValidBriefInViewMessage({...valid, extra: true}, knownParts), false);
"""
        )

    def test_pending_chrome_scroll_accepts_only_its_target_report(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {shouldAcceptInViewReport} = require("./ui/inbox.js");

assert.equal(shouldAcceptInViewReport(null, "scope"), true);
assert.equal(shouldAcceptInViewReport("rationale", "scope"), false);
assert.equal(shouldAcceptInViewReport("rationale", "channels"), false);
assert.equal(shouldAcceptInViewReport("rationale", "rationale"), true);
"""
        )

    def test_v3_draft_fingerprint_is_sorted_and_shape_sensitive(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {
  draftMatchesForm,
  formShapeFingerprint,
  makeDraftRecord,
} = require("./ui/inbox.js");

const form = {zeta: "last", alpha: "first", channels: ["email"]};
assert.equal(
  formShapeFingerprint(form),
  '["alpha","channels","zeta"]',
);
const draft = makeDraftRecord(form);
assert.deepEqual(draft, {
  fingerprint: '["alpha","channels","zeta"]',
  form,
});
assert.equal(draftMatchesForm(draft, {channels: [], zeta: "", alpha: ""}), true);
assert.equal(draftMatchesForm(draft, {zeta: "", alpha: ""}), false);
assert.equal(draftMatchesForm({...draft, extra: true}, form), false);
"""
        )

    def test_same_window_proxy_is_rejected_when_document_is_not_vouched(self) -> None:
        self.run_node(
            r"""
const assert = require("node:assert/strict");
const {isMessageFromCurrentBrief} = require("./ui/inbox.js");
const proxy = {};

assert.equal(isMessageFromCurrentBrief(proxy, proxy, true, 4, 4), true);
assert.equal(isMessageFromCurrentBrief(proxy, proxy, false, 4, 4), false);
assert.equal(isMessageFromCurrentBrief(proxy, proxy, true, 3, 4), false);
assert.equal(isMessageFromCurrentBrief({}, proxy, true, 4, 4), false);
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
    new Response(JSON.stringify({detail: "capture is incomplete"}), {
      status: 400,
      headers: {"Content-Type": "application/json"},
    }),
    "476",
  ));
  assert.equal(submissionFailureKind(rejected), "definitely-not-filed");
  assert.equal(rejected.message, "capture is incomplete");

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
