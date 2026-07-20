# Arachne UI

This directory is the complete application-owned browser UI and is the folder
to import into a design tool. Published decision pages are runtime content and
remain under `pages/`; they are not part of Arachne's shared application shell.

- `inbox.html`, `inbox-content.html`, `brief.html`, `empty.html`, and
  `locked.html` own the inbox markup and three-pane shell.
- `inbox.css` owns its visual system.
- `inbox.js` owns client-side selection, pane resizing, manifest capture,
  per-issue drafts, ruling composition, and filing; the renderer inlines it.
- `fonts/` holds the allowlisted self-hosted display fonts served by the app.
- `bootstrap.html` owns the one-time browser session handoff.
- `render.py` fills the `@@ARACHNE_*@@` slots with authenticated server data.

Keep every `@@ARACHNE_*@@` slot present exactly once in its file. The renderer
fails at startup or render time if that contract drifts. Inbox CSS and JS are
inlined into the authenticated response; only the fixed font allowlist has a
static route.
