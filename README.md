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
   │ tailscaled (userspace, rootless) ── tailscale serve → :8788     │
   │ server.py  (stdlib only, bound 127.0.0.1:8788)                  │
   │   POST /ruling ─notify→ threading.Condition ─release→ GET /wait  │
   │   pages/decision_*.html       ~/.local/state/arachne/rulings/    │
   └─────────────────────────────────────────────────────────────────┘
```

The server binds **loopback only**. Reachability comes entirely from
`tailscale serve`, which exposes it **tailnet-only** over TLS at a stable
MagicDNS name. Authentication is *device identity* — the Tailscale node key is
the credential, so there is no login moment for a human on an enrolled device.

## Security & host-policy posture

- **No public surface.** Nothing listens on the host's public interface;
  `tailscale serve` (never `funnel`) keeps it inside the tailnet.
- **Authenticated by construction.** Only enrolled tailnet devices can reach it,
  so it is not "a public directory service with no authentication" (the one
  Whatbox AUP clause that bears on a hosted service) — it is neither public nor
  unauthenticated.
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
  bin/publish-page.py ← enforces relative POST + localStorage at publish
  bin/install-cron.sh ← idempotently installs the watchdog schedule
  keepalive.sh     ← cron health-check / restart (per DEPLOY)
  pages/           ← served decision pages (content; git-ignored by default)
  tests/           ← real-process end-to-end acceptance tests
```

Rulings and wake cursors live outside the repository by default under
`~/.local/state/arachne/`. Production can set `ARACHNE_DATA_DIR` explicitly.

## Quickstart (local, no Tailscale — proves the wake loop)

```bash
python3 server.py                             # pages here; state outside the repo
# in another shell, arm the wake:
ARACHNE_URL=http://127.0.0.1:8788 bin/arm-wake.sh &
# open http://127.0.0.1:8788/decision_476_relationship_drift.html, submit →
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
condition-variable long poll, and a rootless watchdog. Target host: the Whatbox
seedbox (`proteus.whatbox.ca`, tailnet `tail342046`). The design is host-agnostic
— it can migrate to the home server (`edi-base`) once that node is back on the
tailnet, changing nothing but the node name in the URL.
