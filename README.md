# Arachne

**An agent↔human decision loom.** A featherweight, always-on server that lets an
orchestrating AI agent ask a human a *rich* question — a full interactive HTML
decision page instead of a four-option multiple-choice prompt — and then be
**woken in the same session the instant the human answers**, from any of the
human's devices, with no login friction.

It is the "deluxe upgrade" to a plain `AskUserQuestion`: the agent publishes a
decision page, the human rules asynchronously (phone, tablet, laptop), and the
ruling pushes straight back into the agent's live session. A shared MCP adapter
exposes publication, bootstrap, inspection, and replay-safe waiting as named
tools, so an agent does not need broad shell permission to use the loom.

## Why it exists

Three properties a local `AskUserQuestion` can't offer at once:

1. **Rich prompts.** The question is arbitrary HTML/JS — charts, simulators,
   validated forms, side-by-side previews — not a fixed widget.
2. **Answer from anywhere, asynchronously.** The human isn't tied to the
   terminal; they rule from a phone hours later.
3. **Push-wake, not polling.** The moment a ruling is filed, the agent's session
   resumes with the answer — it does not sit burning turns on a heartbeat.

## How it works (one diagram)

```
[phone / tablet / laptop browser]            [orchestrating agent, its own session]
        │ GET  /  (inbox) · /decision_476.html    ▲ background: GET /wait?since=N
        │ POST /ruling  {issue, markdown, form}    │ exits with the ruling payload →
        ▼ https://arachne.<tailnet>.ts.net         │ harness re-invokes the agent
   ┌─ always-on host (cairn) ──────────────────────┴─────────────────┐
   │ tailscaled (system) ─── tailscale serve ─ verified HTTPS → :8788 │
   │ server.py  (loopback TLS + owner-only application token)          │
   │   POST /ruling ─notify→ threading.Condition ─release→ GET /wait  │
   │   pages/decision_*.html       ~/.local/state/arachne/rulings/    │
   └─────────────────────────────────────────────────────────────────┘
```

The server binds **loopback only**. Remote reachability comes entirely from
`tailscale serve`, which exposes it **tailnet-only** over TLS at a stable
MagicDNS name. Tailscale device identity gates remote access. An additional
owner-only application token gates the loopback listener because loopback is
host-wide on a shared seedbox. The Serve-to-application hop is verified HTTPS:
`tailscaled` trusts a private localhost CA and Arachne holds the corresponding
server key. Merely claiming port 8788 therefore cannot impersonate Arachne and
capture forwarded credentials.

Browsers exchange the token once at `/bootstrap` for a `Secure`, `HttpOnly`,
`SameSite=Strict` cookie whose fifteen-day expiry is enforced by both browser
and server — and **slides on active use**: a session presented past its
half-life is silently re-issued in full, so a device that visits regularly
never re-enrolls while an idle one still ages out. Decision HTML never
contains the secret. The wake client reads the same token from an owner-only
state file and sends it as a bearer credential. Generate a device bootstrap
link with:

```bash
export ARACHNE_PUBLIC_URL=https://arachne.tail342046.ts.net
bin/bootstrap-url.py --open                 # land on the inbox at /
bin/bootstrap-url.py --open decision_476_relationship_drift.html   # deep link
```

**The inbox.** The root path `/` is a stable, bookmarkable mailbox: briefs
awaiting a ruling on top, an archive of ruled ones below. Archive membership is
*derived* — a ruling filed for a page's issue at or after its publication
archives it, and re-publishing the issue reopens it — so submitting a ruling is
itself the archive action and no destructive inbox endpoint exists. A visit
with a lapsed session gets a friendly locked shell that names nothing; ask the
agent for a fresh bootstrap link and the bookmark unlocks itself. Add it to a
phone home screen once and the agent never needs to hand over per-decision
URLs again.

### Install on iPhone or iPad

Arachne is an installable Home Screen web app; it does not need an Xcode
project or Apple Developer Program membership.

1. Connect the device to the tailnet and open a fresh inbox bootstrap link in
   Safari.
2. Wait for the authenticated inbox at `/`, then choose **Share → Add to Home
   Screen** and leave **Open as Web App** enabled.
3. Launch Arachne from its Home Screen icon. Install before starting a draft:
   iOS copies the session cookie into the new web app, but does not copy
   Safari's `localStorage` drafts.

The installed app has its own browser data store. If it sits unused long
enough for its fifteen-day session to lapse, ask the agent for a no-argument
`bootstrap_url()` and paste that complete, single-use inbox enrollment link
into the locked screen. The installed app accepts only a same-origin,
inbox-bound ticket; it never accepts or stores the durable application token.

Arachne deliberately has no service worker, offline cache, notifications, or
badge. It needs the live tailnet service to read or file a ruling. On iOS and
iPadOS, Tailscale's [VPN On Demand](https://tailscale.com/docs/features/client/ios-vpn-on-demand)
can automatically connect for `*.ts.net` hostnames so opening the icon does not
require opening Tailscale first.

## Security & host-policy posture

- **No public surface.** Nothing listens on the host's public interface;
  `tailscale serve` (never `funnel`) keeps it inside the tailnet.
- **Authenticated at both boundaries.** Tailscale authenticates remote devices;
  the application token prevents another account on the shared host from using
  host-wide loopback to read pages, read rulings, or forge one. Private-CA TLS
  prevents a different process on that host from impersonating the loopback
  backend to Tailscale Serve. It is neither public nor unauthenticated.
- **Published pages are privileged code.** Only publish decision HTML from a
  trusted source. A server-supplied Content Security Policy restricts remote
  active content, framing, base URLs, and network connections, but it is defense
  in depth rather than a sanitizer for arbitrary HTML/JavaScript.
- **Featherweight, rootless application, no prohibited category.** stdlib
  `http.server`, no LLM/mining/P2P/Tor. A discipline inherited from the
  shared-seedbox era and kept on `cairn`.
- **No open directory listing.** Pages are served by exact-name allowlist; there
  is no filesystem index. The inbox is a designed, authenticated view — its
  unauthenticated form reveals no names, counts, or rulings.

Full rationale, invariants, and implementer latitude are in [`SPEC.md`](./SPEC.md).

## Repo layout

```
arachne/
  README.md        ← you are here: what & why
  SPEC.md          ← the spec: goal, invariants, behavior, latitude (the handoff)
  DEPLOY.md        ← portable host + Tailscale deployment and migration runbook
  MCP.md           ← shared MCP tools, authentication, and client setup
  server.py        ← the server (created by the implementer, per SPEC)
  ui/              ← importable inbox/bootstrap HTML, CSS, and render boundary
  mcp_server.py    ← authenticated Streamable HTTP MCP adapter
  page_contract.py ← shared validation and atomic publication boundary
  bin/arm-wake.sh  ← the agent-side wake loop (per SPEC)
  bin/bootstrap-url.py ← establishes an authenticated browser session
  bin/publish-page.py ← enforces relative POST + localStorage at publish
  bin/install-cron.sh ← idempotently installs the watchdog schedule
  keepalive.sh     ← cron health-check / restart (per DEPLOY)
  pages/           ← served decision pages (content; git-ignored by default)
  tests/           ← real-process end-to-end acceptance tests
  .claude-plugin/  ← marketplace manifest: this repo installs as a plugin source
  plugin/          ← Claude Code plugin (MCP registration + client skill)
```

The application-owned UI is intentionally self-contained in [`ui/`](./ui/),
including its markup, styling, browser bootstrap, and server-side rendering
boundary. Import that folder when iterating in a design tool. Per-decision HTML
stays in `pages/` because it is published runtime content rather than the shared
application shell.

Rulings, the generated authentication token, and wake cursors live outside the
repository by default under `~/.local/state/arachne/`. Production can set
`ARACHNE_DATA_DIR` or `ARACHNE_TOKEN_FILE` explicitly.

## Quickstart (local, no Tailscale — proves the wake loop)

```bash
ARACHNE_SECURE_COOKIE=false python3 server.py # local HTTP only; production stays Secure
# in another shell, arm the wake:
ARACHNE_URL=http://127.0.0.1:8788 bin/arm-wake.sh &
# bootstrap the browser once, then submit:
bin/bootstrap-url.py --base-url http://127.0.0.1:8788 --open \
  decision_476_relationship_drift.html
# the backgrounded arm-wake.sh prints the ruling JSON and exits.
```

Publish an existing decision page through the contract checker first. It
rewrites the old absolute loopback endpoint to same-origin `/ruling` and fails
loud if the page does not preserve in-progress state:

```bash
bin/publish-page.py /path/to/decision_476_relationship_drift.html
```

Successful `POST /ruling` responses retain the acknowledgement expected by the
existing NEXUS decision pages: `{"ok": true, "filed": "<markdown file>"}`.
The same response also includes the durable entry's sequence, timestamps,
payload, and artifact metadata for newer clients.

Before arming against an unfamiliar cursor, an authenticated agent can inspect
the queued backlog without changing it:

- `GET /rulings?since=N` returns `sequence`, `issue`, and `submitted_at`
  summaries for every ruling after `N`, plus the store's latest sequence.
- `GET /rulings/N` returns the complete persisted ruling at sequence `N`.

These endpoints are read-only. They do not maintain or advance a server-side
cursor, and a later `GET /wait?since=N` still returns the first ruling after
that same cursor. Arachne deliberately exposes no HTTP deletion or reset
operation; state cleanup remains an explicit deployment-administration task.

`examples/phone-smoke.html` is a deliberately synthetic page for the final
phone-to-wake deployment check; it never files a real design ruling.

Run the acceptance suite with:

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
```

To make it always-on and reachable from your phone, follow [`DEPLOY.md`](./DEPLOY.md).
To connect an agent harness without shell access, follow [`MCP.md`](./MCP.md).
Claude Code users can install the client integration in one step — see
[`plugin/README.md`](./plugin/README.md).

## Status

The core uses Python's standard library, flat-file atomic persistence, and a
condition-variable long poll. The sidecar uses the pinned official Python MCP
SDK and keeps no durable cursor state. The application is host-agnostic, but
moving the core remains a real deployment cutover: preserve ruling/cursor
continuity, install destination TLS material, and adapt supervision and
Tailscale ownership to the destination. See [`DEPLOY.md`](./DEPLOY.md).
