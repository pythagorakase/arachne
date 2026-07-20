# Arachne UI

This directory is the complete application-owned browser UI and is the folder
to import into a design tool. Published decision pages are runtime content and
remain under `pages/`; they are not part of Arachne's shared application shell.

- `inbox.html`, `inbox-content.html`, `brief.html`, `empty.html`, and
  `locked.html` own the inbox markup.
- `inbox.css` owns its visual system.
- `bootstrap.html` owns the one-time browser session handoff.
- `render.py` fills the `@@ARACHNE_*@@` slots with authenticated server data.

Keep every `@@ARACHNE_*@@` slot present exactly once in its file. The renderer
fails at startup or render time if that contract drifts. Inbox CSS remains
inlined into the authenticated response so the move adds no public static-file
route and preserves the existing Content Security Policy.
