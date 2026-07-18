# Arachne — Specification

This is a **requirements** spec, written to give the implementer latitude. It
fixes the *goal*, the observable *behavior*, and a short list of *invariants*
that must not be traded away (security, host-policy, and the wake guarantee).
**Everything else — language, framework, file layout, wake transport, storage,
supervision — is the implementer's call.** Where this document shows code or
commands, they are *illustrative*, not binding.

Target host: the Whatbox seedbox (`proteus.whatbox.ca`, tailnet `tail342046`).
Design is host-agnostic. See [`DEPLOY.md`](./DEPLOY.md) for deployment.

---

## 1. Goal

Arachne is an **agent↔human decision loom** — the "deluxe upgrade" to a plain
`AskUserQuestion`. An orchestrating AI agent publishes a *rich* question (a full
interactive HTML page — charts, simulators, validated forms) and is **woken in
the same session the moment the human answers**, from any of the human's
devices, with no login friction.

It must deliver three things a local prompt cannot offer together:

1. **Rich prompts** — the question is arbitrary HTML/JS, not a fixed widget.
2. **Answer from anywhere, asynchronously** — phone/tablet/laptop, hours later.
3. **Push-wake, not polling** — the ruling resumes the agent's live session; the
   agent does not burn turns on a heartbeat.

---

## 2. Invariants (non-negotiable)

These are correctness / safety / host-policy, not implementation taste. Do not
relax them for convenience.

- **Loopback bind only.** The application listens on `127.0.0.1`. It never binds
  a public interface. All external reachability is delegated to the transport
  (§6).
- **Tailnet-only exposure.** Reach it via `tailscale serve` (private, inside the
  tailnet, TLS). **Never `tailscale funnel`** and never a public reverse proxy —
  that would make it a public service.
- **No unauthenticated sensitive surface.** Tailscale device identity gates the
  remote transport, and an owner-only application token gates pages, rulings,
  and waiters on the host-wide loopback interface. Browser sessions use a
  derived `Secure`, `HttpOnly`, `SameSite=Strict` cookie; the token is never
  embedded in published HTML. Nothing may be reachable off the tailnet. (This
  also keeps it clear of the one Whatbox AUP clause that bites hosted services —
  "a public directory service with no authentication.")
- **No directory listing / no traversal.** Serve only an explicit allowlist of
  page files from the pages directory. Never expose a filesystem index or allow
  `..` escapes.
- **Same-session, race-free wake.** A filed ruling must resume the *same* agent
  session (§4), and no ruling may be lost in the window between the agent
  consuming one wake and arming the next. Interval polling is a fallback only,
  never the primary mechanism.
- **Durable, out-of-repo state.** Rulings are persisted durably, outside any
  application source repo.
- **Lightweight & rootless.** No root; no runtime heavy enough to burden a shared
  seedbox; none of the AUP-prohibited categories (LLM inference, mining, P2P
  load-balancing, Tor). A stdlib-only footprint is the safe default.
- **Fail loud.** Errors surface (non-2xx + a diagnostic body / a real
  traceback). No silent fallbacks. *(Owner preference — honor it.)*

---

## 3. Behavioral contract

The implementer chooses names, verbs, and shapes; what follows is the required
*behavior*. (The endpoint names below are the current convention and fine to
keep, but not mandated.)

- **Serve pages.** Given a published decision page, serve it by name over the
  tailnet URL so a browser renders it. Only allowlisted page files; nothing else.
- **Accept a ruling.** Accept a submission carrying at least: an **issue id**, a
  **human-readable record** (markdown), and the **raw form state** (structured).
  Persist it durably as retrievable artifacts.
- **Wake channel.** Provide a way for the agent to *arm* a waiter such that, when
  a new ruling is filed, the waiter returns that ruling's data and the agent's
  session resumes (§4). Must be race-free via a monotonic cursor or equivalent.
- **Health signal.** Provide a cheap liveness/roll-up endpoint for supervision.
- **Page authoring contract.** Pages POST their ruling to a **relative** endpoint
  (same-origin — no CORS needed) and persist in-progress form state to
  `localStorage` (so a phone can resume mid-answer). The existing NEXUS pages
  (`nexus/temp/decision_*.html`) hardcode an absolute `127.0.0.1:8788` endpoint
  today; switching them to a relative path is the one change needed at publish.
  The surrounding server session supplies authentication, so page source never
  carries the application token.

---

## 4. The key design challenge — push-wake

This is the heart of Arachne and the one behavior to get exactly right.

**The harness fact it exploits:** a process the agent launches in the background
re-invokes the agent *when that process exits*. So the job is to convert "a human
filed a ruling" into "the armed process exits, carrying the ruling."

**The race to defeat:** if the agent finishes handling ruling *N* and then
re-arms, a ruling *N+1* that lands in that gap must **not** be missed. The
canonical fix is a **monotonic cursor**: each ruling gets an increasing sequence
number; the agent always re-arms with "give me the first ruling after cursor
*C*," and the server returns immediately if one already exists, otherwise blocks
until one arrives. The cursor must be **restart-safe** — reconstructable from the
persisted rulings so a server restart doesn't replay or drop.

**One proven approach (not binding):** a long-poll `GET /wait?since=C` that
blocks on a condition variable until `POST /ruling` signals it, returns the
ruling + new cursor as JSON, and times out after ~9 min with an empty 204 so the
client reconnects. The client is a background `curl` loop that exits only on a
real ruling. Illustrative core:

```text
server /wait(since):
    lock:
        e = first ruling with seq > since
        if not e: wait(cond, timeout=540s); e = first ruling with seq > since
    return 200 + payload(e)   if e   else 204

client (backgrounded; harness wakes agent on its exit):
    cursor = read persisted cursor
    loop:
        resp = GET /wait?since=cursor           # blocks server-side
        if resp is a ruling: persist new cursor; print ruling; EXIT
        else (204 / transient): reconnect
```

**Latitude:** any transport that yields a clean process-exit wake and is
race-free is acceptable — long-poll, SSE, chunked stream, WebSocket bridged to a
process exit, etc. The cursor may be an integer, a timestamp, a ULID — anything
monotonic and restart-safe.

---

## 5. Implementer's latitude (explicit)

Sol owns these choices — pick what's cleanest:

- **Language / framework** — within the lightweight & rootless invariant. stdlib
  `http.server` is a fine default; asyncio or a micro-framework is fine if it
  stays dependency-light and shared-host-friendly.
- **File/module layout** — single file or a small package.
- **Wake transport** — per §4 latitude.
- **Cursor representation** — per §4 latitude.
- **Ruling storage** — flat files, SQLite, whatever is durable and out-of-repo.
- **Supervision / keep-alive** — screen, tmux, user-systemd, or cron; must
  survive host reboot (see [`DEPLOY.md`](./DEPLOY.md)).
- **Index page** — presence and styling are optional; the agent links pages
  directly.
- **Endpoint names & payload keys** — the §3 shapes are conventions, not law.

---

## 6. Deployment constraints (detail in DEPLOY.md)

- Exposed **tailnet-only** via `tailscale serve`, TLS, at a stable MagicDNS name
  (`arachne.<tailnet>.ts.net`). Never `funnel`.
- `tailscaled` runs **rootless in userspace-networking mode** (no TUN, no root);
  it accepts inbound tailnet connections and proxies to the application-token-
  protected loopback port. Filesystem-permission isolation via a Unix socket is
  unavailable here because Tailscale 1.98.9 requires root/sudo for Serve Unix
  socket targets.
- Enrolling the node is a **one-time human step** (a browser login authorizes the
  node into the tailnet). Flag it; don't try to automate it away.
- The service **survives reboot** (supervised; mechanism is latitude).

---

## 7. Acceptance criteria (behavioral — all real, no mocks)

*(Owner strongly prefers real end-to-end tests over mocks. Exercise the running
service.)*

1. **Health & boot** — service starts and reports liveness.
2. **Page serve + hardening** — an allowlisted page returns; a path-traversal
   attempt and an unknown name return 404; there is no directory index.
3. **File a ruling** — a submission persists durable artifacts (record + form)
   and advances the cursor.
4. **Push-wake (critical)** — arm a waiter, then submit a ruling from a browser;
   the armed process exits within ~1 s carrying the ruling, having done **no
   interval polling**.
5. **Missed-wake race** — submit a ruling *before* arming with the prior cursor;
   the waiter returns the already-filed ruling immediately (no hang, no loss).
6. **Concurrency** — a parked waiter does not block serving a page or accepting a
   new ruling.
7. **Restart-safe cursor** — restart the service; a waiter armed with a
   pre-restart cursor still receives the correct next ruling.
8. **Tailnet-only (negative)** — with the client's Tailscale off, the MagicDNS
   URL does not resolve/answer, and a port scan of the host's public interface
   shows no new open port.
9. **End-to-end from a phone** — load a page over the tailnet URL with no login
   prompt, submit, and confirm the agent wakes.

---

## 8. Out of scope (later)

- A Cloudflare Access variant (only if a non-tailnet consumer ever needs in).
- Migrating the host from the seedbox to `edi-base` once it rejoins the tailnet
  (changes only the node name in the URL).
- Concurrency beyond a single interview / single cursor.
- Folding the ruling pipeline back into the NEXUS repo (kept out-of-repo by
  design).

---

*A fuller worked reference implementation (a complete stdlib `server.py` and
wake-loop) was drafted during design and can be provided on request — but it is
deliberately **not** reproduced here, so the implementer keeps full latitude
over structure.*
