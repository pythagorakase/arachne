# Arachne UI

This directory is the complete application-owned browser UI and is the folder
to import into a design tool. Published decision pages are runtime content and
remain under `pages/`; they are not part of Arachne's shared application shell.

- `inbox.html`, `inbox-content.html`, `brief.html`, `empty.html`, and
  `locked.html` own the inbox markup and three-pane shell.
- `inbox.css` owns its visual system.
- `inbox.js` owns client-side selection, pane resizing, manifest capture,
  per-issue drafts, ruling composition, filing, and the chrome side of the
  brief scroll-sync protocol; the renderer inlines it.
- `brief-scroll-sync.js` is the canonical self-contained reporter for decision
  briefs. Copy the whole file verbatim into an inline script block and place
  `data-axis="<manifest axis id>"` on each per-axis section. It reports the
  topmost visible axis to the parent and accepts scroll requests only from the
  parent; do not load it as an external script from the opaque sandbox.
- `fonts/` holds the allowlisted self-hosted display fonts served by the app.
- `bootstrap.html` owns the one-time browser session handoff.
- `render.py` fills the `@@ARACHNE_*@@` slots with authenticated server data.

Keep every `@@ARACHNE_*@@` slot present exactly once in its file. The renderer
fails at startup or render time if that contract drifts. Inbox CSS and JS are
inlined into the authenticated response; only the fixed font allowlist has a
static route.

`examples/docket-scroll-sync-test.html` embeds the canonical reporter and its
adjacent `.axes.json` file is a publishable v2 manifest for mobile browser
exercise of both message directions.
