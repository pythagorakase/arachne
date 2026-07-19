# Arachne

**An agent↔human decision loom.** A featherweight, always-on server that lets an
orchestrating AI agent ask a human a *rich* question — a full interactive HTML
decision page instead of a four-option multiple-choice prompt — and then be
**woken in the same session the instant the human answers**, from any of the
human's devices, with no login friction.

It is the "deluxe upgrade" to a plain `AskUserQuestion`: the agent publishes a
decision page, the human rules asynchronously (phone, tablet, laptop), and the
ruling pushes straight back into the agent's live session.

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
        │ GET  /decision_476.html                 ▲ background: GET /wait?since=N
        │ POST /ruling  {issue, markdown, form}    │ exits with the ruling payload →
        ▼ https://arachne.<tailnet>.ts.net         │ harness re-invokes the agent
   ┌─ always-on host (seedbox today) ──────────────┴─────────────────┐
   │ tailscaled (rootless) ─ tailscale serve ─ verified HTTPS → :8788 │
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
`SameSite=Strict` cookie whose two-day expiry is enforced by both browser and
server; decision HTML never contains the secret. The wake client reads the same
token from an owner-only state file and sends it as a bearer credential.
Generate a device bootstrap link with:

```bash
export ARACHNE_PUBLIC_URL=https://arachne.tail342046.ts.net
bin/bootstrap-url.py --open decision_476_relationship_drift.html
```

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
- **Featherweight, rootless, no prohibited category.** stdlib `http.server`,
  userspace `tailscaled`, no root, no LLM/mining/P2P/Tor. Well inside a shared
  seedbox's rules.
- **No open directory listing.** Pages are served by exact-name allowlist; there
  is no filesystem index.

Full rationale, invariants, and implementer latitude are in [`SPEC.md`](./SPEC.md).

## Repo layout

```
arachne/
  README.md        ← you are here: what & why
  SPEC.md          ← the spec: goal, invariants, behavior, latitude (the handoff)
  DEPLOY.md        ← seedbox + Tailscale runbook (one human step, flagged)
  server.py        ← the server (created by the implementer, per SPEC)
  bin/arm-wake.sh  ← the agent-side wake loop (per SPEC)
  bin/bootstrap-url.py ← establishes an authenticated browser session
  bin/publish-page.py ← enforces relative POST + localStorage at publish
  bin/install-cron.sh ← idempotently installs the watchdog schedule
  keepalive.sh     ← cron health-check / restart (per DEPLOY)
  pages/           ← served decision pages (content; git-ignored by default)
  tests/           ← real-process end-to-end acceptance tests
```

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

`examples/phone-smoke.html` is a deliberately synthetic page for the final
phone-to-wake deployment check; it never files a real design ruling.

Run the acceptance suite with:

```bash
python3 -m unittest discover -s tests -v
```

To make it always-on and reachable from your phone, follow [`DEPLOY.md`](./DEPLOY.md).

## Status

Implemented with Python's standard library, flat-file atomic persistence, a
condition-variable long poll, and a rootless watchdog. The temporary target is
the Whatbox seedbox (`proteus.whatbox.ca`, tailnet `tail342046`). The application
is host-agnostic, but moving it to the home server (`edi-base`) is a real
deployment cutover: preserve ruling/cursor continuity, install destination TLS
material, and adapt from a user-owned rootless Tailscale daemon to the host's
system Tailscale service. See [`DEPLOY.md`](./DEPLOY.md).
