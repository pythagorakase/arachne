# Arachne — Specification

This is a **requirements** spec, written to give the implementer latitude. It
fixes the *goal*, the observable *behavior*, and a short list of *invariants*
that must not be traded away (security, host-policy, and the wake guarantee).
**Everything else — language, framework, file layout, wake transport, storage,
supervision — is the implementer's call.** Where this document shows code or
commands, they are *illustrative*, not binding.

Target host: `cairn`, a home Ubuntu box on tailnet `tail342046` (originally
the Whatbox seedbox `proteus.whatbox.ca`, retired 2026-07-19). Design is
host-agnostic. See [`DEPLOY.md`](./DEPLOY.md) for deployment.

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

- **Loopback bind only.** Every Arachne listener uses `127.0.0.1`; none binds a
  public interface. All external reachability is delegated to narrowly scoped
  transports (§6).
- **Tailnet-only interactive application.** Reach the inbox, briefs, sessions,
  rulings, publication, and waiters via `tailscale serve` (private, inside the
  tailnet, TLS). **Never `tailscale funnel`** and never put `server.py` behind a
  public reverse proxy. The only public surface is the separate inert-snapshot
  origin described below; it cannot route to the interactive server.
- **Authenticated proxy-to-app hop.** Tailscale Serve connects to Arachne over
  verified HTTPS using a private localhost CA. The Serve proxy trusts that CA;
  Arachne holds the corresponding server key and certificate. A different user
  merely claiming the loopback port must not be able to impersonate the backend
  or receive forwarded cookies, bearer credentials, or bootstrap fragments.
- **No unauthenticated sensitive surface.** Tailscale device identity gates the
  remote transport, and an owner-only application token gates pages, rulings,
  and waiters on the host-wide loopback interface. Browser sessions use a
  server-validated `Secure`, `HttpOnly`, `SameSite=Strict` cookie with a
  fifteen-day window that **slides on active use**: a session presented past
  its half-life is re-issued for the full window, so a regularly used device
  never re-enrolls while an idle or lost one still ages out. The token is
  never embedded in published HTML. Expiry must be enforced by the server, not
  only by the browser's cookie lifetime. No page, ruling, inbox datum, token,
  draft, or waiter may be reachable off the tailnet. A snapshot becomes public
  only through an explicit owner share action and contains inert semantic
  content rather than application state.
- **Capability-only public snapshots.** Public links use at least 192 random
  bits, expire server-side after 30 days, and can be revoked sooner. Unknown,
  expired, and revoked identifiers are indistinguishable. HTML and Markdown
  are generated from one allowlisted semantic tree with no script, live form,
  storage, cookie, or authenticated-resource access. The public process knows
  no application token and serves no directory, page, ruling, or proxy route.
- **No directory listing / no traversal.** Serve only an explicit allowlist of
  page files from the pages directory. Never expose a filesystem index or allow
  `..` escapes.
- **Same-session, race-free wake.** A filed ruling must resume the *same* agent
  session (§4), and no ruling may be lost in the window between the agent
  consuming one wake and arming the next. Interval polling is a fallback only,
  never the primary mechanism.
- **Durable, out-of-repo state.** Rulings are persisted durably, outside any
  application source repo.
- **Lightweight & unprivileged application.** Arachne and its watchdog never run
  as root and use no runtime heavy enough to burden a shared seedbox. A normal
  host may use its existing root-managed system `tailscaled`; Arachne does not
  own that daemon. None of the AUP-prohibited categories (LLM inference, mining,
  P2P load-balancing, Tor) apply. A stdlib-only footprint is the safe default.
- **Fail loud.** Errors surface (non-2xx + a diagnostic body / a real
  traceback). No silent fallbacks. *(Owner preference — honor it.)*
- **Published pages are trusted privileged code.** A decision page is part of
  the application, not untrusted user content. Publish only reviewed HTML/JS.
  Apply a server-controlled Content Security Policy that restricts remote active
  content, framing, base URLs, and network connections; treat it as defense in
  depth, not as an HTML sanitizer.

---

## 3. Behavioral contract

The implementer chooses names, verbs, and shapes; what follows is the required
*behavior*. (The endpoint names below are the current convention and fine to
keep, but not mandated.)

- **Serve pages.** Given a published decision page, serve it by name over the
  tailnet URL so a browser renders it. Only allowlisted page files; nothing else.
- **Inbox at the root.** The stable root path is an authenticated mailbox:
  briefs awaiting a ruling, and an archive **derived** from filed rulings — a
  ruling carrying a page's issue token, filed at or after that page's
  publication, archives it; re-publishing the issue reopens it. The pairing
  token is **recorded at publication** (the publish operation accepts the
  issue the page will file); filename inference is only a fallback for pages
  published without one, since a valid page name carries no reliable issue.
  Filing the ruling *is* the archive action: no deletion, move, or other
  mutating inbox operation may exist. The unauthenticated root must stay
  human-friendly for a lapsed bookmark while revealing nothing — no page
  names, counts, or rulings.
- **Accept a ruling.** Accept a submission carrying at least: an **issue id**, a
  **human-readable record** (markdown), and the **raw form state** (structured).
  Persist it durably as retrievable artifacts.
- **Wake channel.** Provide a way for the agent to *arm* a waiter such that, when
  a new ruling is filed, the waiter returns that ruling's data and the agent's
  session resumes (§4). Must be race-free via a monotonic cursor or equivalent.
- **Health signal.** Provide a cheap liveness/roll-up endpoint for supervision.
- **Page authoring contract.** A trusted page owns its argument and capture
  form, but the authenticated application chrome owns device-local drafts and
  the relative `POST /ruling`. The page embeds the canonical capture agent and
  must not reference `/ruling` or `localStorage` itself. Every substantive
  visual supplies `data-arachne-llm-alt` semantic content (or is explicitly
  decorative), so public snapshot generation cannot silently omit evidence.
- **Semantic share.** An authenticated owner can create a public, read-only
  snapshot of a published page without sharing current draft state. The action
  returns equivalent HTML and Markdown capability URLs, an expiry, and a
  content hash. Repeating or revoking the action never changes ruling/archive
  semantics.

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

Operational inspection must not impersonate consumption. An authenticated
client may list summaries after a cursor or peek at a persisted ruling, but
those reads must not change the cursor or alter what a subsequent waiter
receives. Destructive backlog maintenance is a separate administrative action,
not part of the decision or wake API.

**Latitude:** any transport that yields a clean process-exit wake and is
race-free is acceptable — long-poll, SSE, chunked stream, WebSocket bridged to a
process exit, etc. The cursor may be an integer, a timestamp, a ULID — anything
monotonic and restart-safe.

An MCP transport must preserve that contract rather than hide it: the wait tool
takes an explicit cursor, returns the advancing cursor with the ruling, and
emits progress notifications frequently enough to keep a live human wait from
looking like an idle or wedged tool call. Repeating a wait with the same cursor
must replay the same first unseen ruling.

Decision-page publication is also an authenticated server capability. Remote
tooling sends trusted HTML to the server-side publication boundary, which
enforces the canonical page contract and commits atomically; it must not rely
on shelling out to `rsync` or on granting the harness general filesystem access.

---

## 5. Implementer's latitude (explicit)

Sol owns these choices — pick what's cleanest:

- **Language / framework** — within the lightweight, unprivileged-application
  invariant. stdlib `http.server` is a fine default; asyncio or a micro-framework
  is fine if it stays dependency-light and shared-host-friendly.
- **File/module layout** — single file or a small package.
- **Wake transport** — per §4 latitude.
- **Cursor representation** — per §4 latitude.
- **Ruling storage** — flat files, SQLite, whatever is durable and out-of-repo.
- **Supervision / keep-alive** — screen, tmux, user-systemd, or cron; must
  survive host reboot (see [`DEPLOY.md`](./DEPLOY.md)).
- **Inbox presentation** — the §3 inbox behavior is required, but its styling,
  title extraction, ordering, and issue-derivation details are the
  implementer's; the agent may still link pages directly.
- **Endpoint names & payload keys** — the §3 shapes are conventions, not law.

---

## 6. Deployment constraints (detail in DEPLOY.md)

- Exposed **tailnet-only** via `tailscale serve`, TLS, at a stable MagicDNS name
  (`arachne.<tailnet>.ts.net`). Never `funnel`. The proxy target is verified
  HTTPS using a private localhost CA; an insecure HTTPS target is not allowed.
- Public snapshots use a **different hostname and loopback target**. A public
  reverse proxy or outbound tunnel may route `share.pythagora.net` only to
  `share_server.py`; it must never have a route to `server.py`, the MCP adapter,
  or their ports. The public process requires no inbound port when an
  outbound-only tunnel is used.
- A **custom domain may front the same service without changing the exposure
  invariant**: a DNS-only record (never a proxying CDN) pointing at the node's
  tailnet address, terminated by a TLS proxy **bound exclusively to tailnet
  interfaces**, with certificates issued via DNS-01 so no public listener ever
  exists. The ts.net name remains valid in parallel; browser sessions are
  per-hostname, so devices should bookmark one canonical host. Public reverse
  proxies and Funnel remain forbidden.
- On the shared seedbox, `tailscaled` runs **rootless in userspace-networking
  mode** (no TUN, no root). On a normal Ubuntu destination such as `cairn`,
  use its system-managed `tailscaled` and grant the deployment account the
  narrow Tailscale operator access needed to manage Serve. These are distinct
  deployment topologies, not a node-name-only substitution.
- Filesystem-permission isolation via a Unix socket is unavailable on the
  seedbox because Tailscale 1.98.9 requires root/sudo for Serve Unix socket
  targets. Private-CA verification supplies backend identity on loopback.
- Enrolling the node is a **one-time human step** (a browser login authorizes the
  node into the tailnet). Flag it; don't try to automate it away.
- The service **survives reboot** (supervised; mechanism is latitude).

---

## 7. Acceptance criteria (behavioral — all real, no mocks)

*(Owner strongly prefers real end-to-end tests over mocks. Exercise the running
service.)*

1. **Health & boot** — service starts and reports liveness.
2. **Page serve + hardening** — an allowlisted page returns with the server's
   restrictive decision-page CSP; a path-traversal attempt and an unknown name
   return 404; there is no directory index.
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
10. **Backend identity + session expiry** — Serve reaches Arachne through a
    private-CA-verified HTTPS target and rejects an untrusted replacement
    backend; a cryptographically valid but expired browser session is denied by
    the server.
11. **Inbox derivation** — an authenticated root lists an unruled page as
    awaiting; filing its ruling moves it to the archive with no other request;
    re-publishing the same issue returns it to awaiting. An unauthenticated
    root request gets the friendly shell and leaks no names or counts.
12. **Sliding session** — a valid session past its half-life is transparently
    re-issued for the full window on use; a fresh session is not; bearer
    requests never mint cookies.
13. **Inbox bootstrap** — a bootstrap link minted without a page lands the
    device on the inbox; an inbox-bound ticket cannot unlock a page-bound
    session or vice versa; the ticket stays single-use.
14. **Semantic sharing** — one authenticated action produces exact public HTML
    and Markdown capability paths with the same prose, option explanations,
    tables, and LLM visual equivalents; neither artifact contains script,
    forms, drafts, or authenticated URLs. Both disappear at 30 days and
    immediately after revocation, while every other public-server path is 404.

---

## 8. Out of scope (later)

- A Cloudflare Access variant (only if a non-tailnet consumer ever needs in).
- Concurrency beyond a single interview / single cursor.
- Folding the ruling pipeline back into the NEXUS repo (kept out-of-repo by
  design).

---

*A fuller worked reference implementation (a complete stdlib `server.py` and
wake-loop) was drafted during design and can be provided on request — but it is
deliberately **not** reproduced here, so the implementer keeps full latitude
over structure.*
