# Arachne Nav-Pane Protocol — Current Specification

**Status:** Supersedes the earlier axis-manifest Ruling Docket draft. The
implemented v2 contract has no axis manifest: the brief owns both its argument
and capture form, while the application chrome supplies a companion `<nav>` and
mediates Send.

## 1. Load-bearing server invariant

The storage and wake protocol does not change:

- `/ruling` accepts `{issue, markdown, form}`. `form` remains an opaque object
  and `markdown` must be non-empty.
- `RulingStore`, persisted artifacts, `/wait`, cursors, and the MCP consumer are
  untouched.
- A ruling carrying a published page's recorded issue archives that brief;
  republishing it later reopens it.

This is a producer and browser-shell change, not a ruling protocol change.

## 2. Published brief contract

A v2 brief is a self-contained argument **plus its own capture `<form>`**:

- Put the issue token on `<html data-issue="…">` or
  `<body data-issue="…">`.
- Use any named `input`, `select`, or `textarea` controls needed for the
  decision.
- Wrap each required decision part in a
  `[data-decision="<stable-id>"]` element. An optional `data-label` supplies its
  display/record label.
- Embed the complete, unmodified `ui/brief-agent.js` in an inline
  `<script data-arachne-brief-agent>` immediately before `</body>`.
- Keep the page self-contained. It executes in the opaque iframe sandbox
  `sandbox="allow-scripts"`.

There is no sidecar axis schema and no manifest endpoint. The publication
contract rejects legacy `/ruling` and `localStorage` references in brief HTML.
The brief never files or persists a draft directly; the chrome owns both.

`page_contract.prepare_html` enforces the forbidden-reference and filename
rules. The local producer reads `data-issue` and publishes through the same
`publish_html` path:

```bash
bin/publish-page.py source.html --pages-dir /path/to/pages
```

Multiple source paths are allowed. `--issue` is a single-source override only;
without it, every source must declare its own `data-issue`.

## 3. Brief agent responsibilities

The canonical inline agent:

- finds ordered `[data-decision]` parts and reports their labels/completeness;
- serializes named controls into the opaque `form` object;
- composes readable default markdown in part order, unless a brief supplies a
  valid `arachneCaptureHooks.composeMarkdown` hook;
- accepts chrome-side restore and scroll requests; and
- reports the part currently in view for desktop/phone navigation.

`data-answered` and `arachneCaptureHooks.isAnswered` are trusted completeness
overrides. A brief using them is responsible for not treating a meaningless
ruling as complete. The generic composer writes `(no value)` for an answered
part with no serialized value, and `— (unanswered)` only for an unanswered
part.

## 4. Chrome companion and fresh Send

The application-owned ruling companion is a `<nav>`, not a second capture
form. It renders part progress, synchronizes navigation with the framed brief,
persists device-local drafts, and is the only browser code that POSTs a ruling.

Send uses a fresh request/response handshake rather than the last animation-
frame-batched capture:

```js
{source: "arachne-chrome", type: "collect", token: "<unique string>"}

{source: "arachne-brief", type: "ruling", token: "<same string>",
 form: {...}, markdown: "...", allAnswered: true}
```

Both messages require those exact keys. The brief handles `collect`
synchronously: it immediately reserializes the form, recomputes parts and
markdown, and replies. The chrome accepts `ruling` only from the selected
iframe's current vouched document and only for the pending token. An incomplete
reply is definitely not filed. A missing reply times out as uncertain; the
chrome neither falls back to an older capture nor retries automatically.

## 5. Document generation and drafts

An iframe `WindowProxy` survives navigation, so source equality alone does not
identify a document. The chrome marks the next iframe load as expected whenever
it assigns `frame.src`. A later unprompted load is foreign/self-navigation: the
selected card's capture is invalidated and all brief messages are ignored until
the card is re-selected and chrome-loaded again.

Drafts use `arachne:draft:v3:<issue>`. Each stored record contains the opaque
`form` plus a fingerprint made from its sorted control-name keys. Restore is
deferred until the first capture from the newly loaded document; the chrome
restores only when fingerprints match. A mismatch is discarded. Restore only
sets names present in the saved form, preserving authored defaults for every
other control.

## 6. Layout and security boundary

- Desktop keeps the three-pane list / reading iframe / ruling-nav layout.
- Phone keeps its distinct inbox-to-reading flow and fixed ruling ribbon; the
  ribbon is a compact view of the same part navigation and Send state.
- The iframe sandbox remains exactly `sandbox="allow-scripts"`: no
  `allow-same-origin` and no `allow-forms`.
- The locked shell discloses neither brief metadata nor the authenticated
  inbox client.

## 7. Out of scope

- Changes to `/ruling` acceptance, `RulingStore`, `/wait`, cursor semantics, or
  MCP consumption.
- Interpreting or normalizing the opaque `form` server-side.
- Moving capture controls into the chrome or reintroducing a manifest.
- Collapsing the phone layout into the desktop fallback.
