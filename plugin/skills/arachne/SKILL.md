---
name: arachne
description: Put a rich, interactive decision to the human via Arachne and get woken in the SAME session when they answer from any device (phone/tablet/laptop). Use when a choice is richer than a few options (charts, simulators, side-by-side previews, validated forms) and/or should be answered asynchronously from anywhere, instead of a synchronous in-terminal prompt. Also triggers on "ask me on my phone", "put this to me asynchronously", "wake me when I decide/answer", or any mention of Arachne / a decision page / the decision loom. NOT for quick synchronous choices you need answered right now — use AskUserQuestion for those.
---

# Arachne — Async Decision Loom (MCP Client Skill)

Arachne is an always-on, tailnet-only server: the agent publishes a rich HTML
decision page, the human rules from any device, and the ruling completes a
pending tool call in this very session — no polling, no heartbeat management.

This plugin registers Arachne's shared MCP adapter as server `arachne`. Five
tools (surfaced as `mcp__plugin_arachne_arachne__<tool>`):

| Tool | Effect |
|------|--------|
| `status(since)` | Health + non-destructive backlog summaries after `since`. |
| `get_ruling(sequence)` | Read one complete persisted ruling; no cursor change. |
| `wait_for_ruling(since)` | Block until the first ruling after `since`; returns `{cursor, ruling}`. |
| `publish_decision(name, html)` | Server-side contract validation + atomic publish; returns the page URL. |
| `bootstrap_url(page)` | Mint a single-use, short-lived browser URL for the human. |

## When to Use It (and When Not)

- **Use** when the decision benefits from a real page (visuals, a simulator,
  several rich options), the human should answer from a phone or hours later,
  and you want to go idle and resume automatically on their answer.
- **Don't use** for a quick synchronous choice needed in-terminal right now —
  that is `AskUserQuestion`. Arachne is for *asynchronous, rich,
  answer-from-anywhere* decisions.

## The Cursor

The durable wake cursor lives at `~/.local/state/arachne/cursor` (shared with
the shell client; missing file means `0`). Read it before waiting; write the
`cursor` returned by `wait_for_ruling` back to the file **after acting on the
ruling** — replaying a ruling is safe, losing one is not. Never hand-edit it
otherwise.

## Workflow

0. **Preflight** — call `status` with the current cursor as `since`. Expect
   `health.ok: true`; the `backlog` block lists any rulings already queued
   past your cursor (handle those first via `get_ruling`). On error, stop and
   report: server, Tailscale, or token trouble — do not improvise fallbacks.
1. **Author** a self-contained `decision_<slug>.html` per the page contract
   below.
2. **Publish** with `publish_decision(name, html)`. Validation is
   server-side (relative `/ruling` endpoint, `localStorage` persistence,
   name allowlist `[A-Za-z0-9][A-Za-z0-9._-]*\.html`); it rewrites absolute
   loopback endpoints and publishes atomically. Returns the public page URL.
3. **Hand over the link** — `bootstrap_url(page)` returns a **single-use**
   URL that expires in minutes (`expires_at` is in the result). Mint it when
   the human is ready to tap, send it to them, and mint a fresh one if it
   lapses. Opening it sets their session cookie and lands on the page; the
   ticket rides in the URL fragment and never appears in logs.
4. **Arm the wake** — call `wait_for_ruling(since=<cursor>)` and then end
   your turn (or continue other work). This is a long MCP call: the harness
   auto-backgrounds it into a task after a couple of minutes, the server's
   progress heartbeats keep the idle timeout at bay, and the adapter itself
   rides through the core's long-poll cycles and outages with backoff. When
   the human submits, the call completes with `{cursor, ruling}` and you are
   re-invoked. Do NOT poll `status` in a loop and do NOT schedule wakeups —
   the completing call IS the wake.
5. **On wake** — act on `ruling` (`issue`, `markdown`, `form`,
   `submitted_at`), then persist the returned `cursor`. More decisions
   pending? Re-check `status` and wait again with the new cursor.

## Page Contract

- **Self-contained.** Inline `<style>`/`<script>` only; the server applies a
  strict CSP (no external hosts; `data:`/`blob:` allowed for assets).
- **Submit to the relative endpoint:**

  ```js
  fetch('/ruling', { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ issue, markdown, form }) })
  ```

  `issue`: short id string; `markdown`: human-readable record of the choice;
  `form`: the structured answer object.
- **Persist in-progress state to `localStorage`** so a phone can resume
  mid-answer.
- On success show a confirmation; on `!response.ok`, surface the error text.

## Gotchas

- **Permissions.** Unprompted auto-mode use requires
  `"mcp__plugin_arachne_arachne__*"` in `permissions.allow` (user settings).
  The `mcp__plugin_...` prefix is specific to plugin-bundled servers.
- **Token.** The connect-time helper reads
  `~/.local/state/arachne/auth-token` (override: `ARACHNE_TOKEN_FILE`, or set
  it in `~/.config/arachne/env`). Missing or empty file → the server shows as
  failed/disconnected. Copy it once from the server host and `chmod 600`.
- **Endpoint.** Defaults to the author's deployment; override with
  `ARACHNE_MCP_URL` in the environment that launches Claude Code.
- **Replay safety.** `wait_for_ruling` with the same `since` returns the same
  first-ruling-after — safe across drops and re-calls. Only advance the
  cursor file after acting.
- **Host migration.** New deployments start at sequence 0 — reset the cursor
  file to `0` when the server's store is fresh.
- **Shell fallback.** For contexts without MCP, the checkout's `bin/`
  (`arm-wake.sh`, `publish-page.py`, `bootstrap-url.py`) with
  `~/.config/arachne/env` still implements the same workflow over HTTPS.
