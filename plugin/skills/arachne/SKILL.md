---
name: arachne
description: Put a rich, interactive decision to the human via Arachne and get woken in the SAME session when they answer from any device (phone/tablet/laptop). Use when a choice is richer than a few options (charts, simulators, side-by-side previews, validated forms) and/or should be answered asynchronously from anywhere, instead of a synchronous in-terminal prompt. Also triggers on "ask me on my phone", "put this to me asynchronously", "wake me when I decide/answer", or any mention of Arachne / a decision page / the decision loom. NOT for quick synchronous choices you need answered right now — use AskUserQuestion for those.
---

# Arachne — Async Decision Loom (MCP Client Skill)

Arachne's interactive application is always-on and tailnet-only: the agent
publishes a rich HTML decision page, the human rules from any device, and the
ruling completes a pending tool call in this very session — no polling, no
heartbeat management. Optional public links are inert 30-day snapshots served
by a separate process; they never expose the application origin.

This plugin registers Arachne's shared MCP adapter as server `arachne`. Five
tools (surfaced as `mcp__plugin_arachne_arachne__<tool>`):

| Tool | Effect |
|------|--------|
| `status(since)` | Health + non-destructive backlog summaries after `since`. |
| `get_ruling(sequence)` | Read one complete persisted ruling; no cursor change. |
| `wait_for_ruling(since)` | Block until the first ruling after `since`; returns `{cursor, ruling}`. |
| `publish_decision(name, html, issue?)` | Server-side contract validation + atomic publish; returns the page URL. Pass the issue the page files. |
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
2. **Publish** with `publish_decision(name, html, issue)`, where `issue` is
   the same token as the brief's `data-issue` — recording it is what lets the
   inbox archive the brief the moment its ruling is filed. Validation is
   server-side: the name must match
   `[A-Za-z0-9][A-Za-z0-9._-]*\.html`, legacy capture references are rejected,
   and publication is atomic. The tool returns the public page URL. From a
   checkout, the equivalent producer path is:

   ```bash
   bin/publish-page.py decision_<slug>.html --pages-dir /path/to/arachne/pages
   ```
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

The current model is **argument plus brief-owned capture, chrome-owned filing,
and a semantic public-share source**:

- Make the brief self-contained. Inline all CSS, assets, and scripts; the page
  runs in an opaque `sandbox="allow-scripts"` iframe.
- Put the ruling controls in the brief's own `<form>`. Any named `input`,
  `select`, or `textarea` controls are allowed.
- Wrap every independently required decision part in an element with
  `data-decision="<stable-id>"`. `data-label="Human label"` is optional; without
  it the agent derives a label from the part's heading/text.
- Put the issue token on `<html data-issue="…">` or `<body data-issue="…">`.
- Give every substantive visual an LLM-readable text equivalent. A `<figure>`,
  `<img>`, `<canvas>`, `<svg role="img">`, or custom interactive region marked
  `data-arachne-visual` must have an LLM alternative. An ordinary image may
  use a non-empty `alt="…"`; a simple custom visual may use
  `data-arachne-llm-alt="…"` directly. Charts, diagrams, and simulations
  should place semantic HTML in an inert descendant
  `<template data-arachne-llm-alt>` describing the relevant values,
  relationships, selectable states, and conclusion. Mark purely decorative
  visuals `aria-hidden="true"` instead.
- Immediately before `</body>`, add an inline
  `<script data-arachne-brief-agent>` whose body is the **verbatim, complete
  contents** of `ui/brief-agent.js`. Do not rewrite or externally load it.
- A v2 brief MUST NOT contain `/ruling` or `localStorage`. The application
  chrome owns draft persistence, requests a fresh capture on Send, and mediates
  the POST.

Minimal structure (the comment marks where the unmodified canonical agent must
be pasted; `examples/nav-capture-test.html` is the complete runnable example):

```html
<!doctype html>
<html lang="en" data-issue="476">
<head>
  <meta charset="utf-8">
  <title>Choose the rollout</title>
</head>
<body>
  <main>
    <h1>Choose the rollout</h1>
    <p>The argument and evidence belong here.</p>
    <figure data-arachne-visual>
      <!-- chart, diagram, or simulation -->
      <template data-arachne-llm-alt>
        <p>The pilot reaches one team. The broad rollout reaches all four
        teams and requires twice the support capacity.</p>
      </template>
    </figure>
    <form>
      <section data-decision="scope" data-label="Rollout scope">
        <h2>Scope</h2>
        <label><input type="radio" name="scope" value="pilot"> Pilot</label>
        <label><input type="radio" name="scope" value="broad"> Broad</label>
      </section>
    </form>
  </main>
  <script data-arachne-brief-agent>
  /* Paste the complete contents of ui/brief-agent.js here verbatim. */
  </script>
</body>
</html>
```

The canonical agent serializes the named controls as opaque `form`, composes a
readable default `markdown` record (or honors the brief's capture hooks), and
reports completeness. The surrounding `<nav>` companion owns progress, local
drafts, and the one-ruling Send operation.

The inbox's Share control creates an inert snapshot from this semantic source.
It never includes scripts, draft state, submission controls, cookies, or
storage. The public HTML and `.md` forms contain the same information, and the
capability expires automatically after 30 days.

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
