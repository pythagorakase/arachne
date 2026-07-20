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
| `bootstrap_url(page?)` | Mint a single-use, short-lived browser URL; omit `page` to land on the inbox at `/`. |

## When to Use It (and When Not)

- **Use** when the decision benefits from a real page (visuals, a simulator,
  several rich options), the human should answer from a phone or hours later,
  and you want to go idle and resume automatically on their answer.
- **Don't use** for a quick synchronous choice needed in-terminal right now —
  that is `AskUserQuestion`. Arachne is for *asynchronous, rich,
  answer-from-anywhere* decisions.

## The Cursor

The durable wake cursor is shared with the shell client and resolved by the
same rule as `bin/arm-wake.sh`: `$ARACHNE_CURSOR_FILE` if set, else
`${XDG_STATE_HOME:-$HOME/.local/state}/arachne/cursor`. A missing file means
`0`. Read it before waiting; write the `cursor` returned by `wait_for_ruling`
back to the file **after acting on the ruling** — replaying a ruling is safe,
losing one is not. Never hand-edit it otherwise.

## Workflow

0. **Preflight** — call `status` with the current cursor as `since`. Expect
   `health.ok: true`. If the `backlog` block lists rulings already queued past
   your cursor, **drain them through `wait_for_ruling` first** — it returns
   instantly while a queued ruling exists, and every consumption then comes
   with an advancing `cursor` to persist after acting. `get_ruling` is a
   read-only peek (re-reading, auditing); it advances nothing, so never use
   it as the consumption path. On error, stop and report: server, Tailscale,
   or token trouble — do not improvise fallbacks.
1. **Author** a self-contained `decision_<slug>.html` per the page contract
   below.
2. **Publish** with `publish_decision(name, html)`. Validation is
   server-side (relative `/ruling` endpoint, `localStorage` persistence,
   name allowlist `[A-Za-z0-9][A-Za-z0-9._-]*\.html`); it rewrites absolute
   loopback endpoints and publishes atomically. Returns the public page URL.
3. **Point at the inbox** — the human's devices hold a fifteen-day sliding
   session and a bookmark to the stable inbox at `/`, where the new brief is
   already listed. Default to saying the brief is **in their Arachne inbox**
   (name it; optionally include the plain inbox URL — it carries no secret).
   Only mint `bootstrap_url()` when a device needs enrollment — it is new, or
   its session lapsed (~15 idle days) and the inbox shows the locked shell.
   The result is a **single-use** URL that expires in minutes (`expires_at`
   is in the result): no-arg lands on the inbox; `bootstrap_url(page)`
   deep-links one brief. The ticket rides in the URL fragment and never
   appears in logs.
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
   pending? Wait again with the new cursor — each `wait_for_ruling` returns
   instantly while the backlog is non-empty.

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
- **Token.** The connect-time helper resolves
  `${XDG_STATE_HOME:-$HOME/.local/state}/arachne/auth-token` (override:
  `ARACHNE_TOKEN_FILE`, or set it in `~/.config/arachne/env`). The token file
  — and the config file, because it is sourced — must be owner-only
  (`chmod 600`); the helper refuses group/other-accessible files, symlinks,
  and tokens outside the server's `[A-Za-z0-9_-]{32,256}` grammar, and the
  server then shows as failed/disconnected. Copy the token once from the
  server host.
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
