# Arachne UI

This directory is the complete application-owned browser UI and is the folder
to import into a design tool. Published decision pages are runtime content and
remain under `pages/`; they are not part of Arachne's shared application shell.

- `inbox.html`, `inbox-content.html`, `brief.html`, `empty.html`, and
  `locked.html` own the inbox markup and three-pane shell.
- `inbox.css` owns its visual system.
- `inbox.js` owns client-side selection, pane resizing, the decision `<nav>`,
  per-issue drafts, mediated filing, and the chrome side of the brief message
  protocol; the renderer inlines it.
- `brief-agent.js` is the canonical self-contained capture and scroll-sync
  agent for decision briefs. Copy the whole file verbatim into an inline script
  block and place `data-decision="<part id>"` on each decision section. It
  reports parts, progress, serialized form state, and brief-composed markdown
  to the parent, accepts restore/scroll requests only from the parent, and
  answers a tokened `collect` request with an immediate fresh ruling snapshot;
  do not load it as an external script from the opaque sandbox.
- `fonts/` holds the allowlisted self-hosted display fonts served by the app.
- `bootstrap.html` owns the one-time browser session handoff.
- `render.py` fills the `@@ARACHNE_*@@` slots with authenticated server data.

Keep every `@@ARACHNE_*@@` slot present exactly once in its file. The renderer
fails at startup or render time if that contract drifts. Inbox CSS and JS are
inlined into the authenticated response; only the fixed font allowlist has a
static route.

`examples/nav-capture-test.html` embeds the canonical agent and exercises
radio, multi-value checkbox, and textarea capture without a manifest.
