# Arachne Docket Protocol — Specification (Draft for Ruling)

**Status:** Frozen — ready to hand to Sol. Fixes the contract for moving ruling
capture out of the brief document and into Arachne chrome (the "Ruling Docket"
redesign). All six decisions (§7) are resolved.

## 0. Codebase note (targets the `ui/` package, post-refactor)

Since this spec was drafted, the UI was extracted into an importable `ui/`
package (PR #11, "Move the Application UI Into a Dedicated Package"). Re-map
every "in `server.py`" reference below to that structure:
- **Inbox / shell rendering** lives in `ui/render.py` (`render_inbox`,
  `render_locked_inbox`, `render_bootstrap`) filling `@@ARACHNE_*@@` slots in
  `ui/inbox.html`, `ui/inbox-content.html`, `ui/brief.html`, `ui/inbox.css`.
  The three-pane shell, phone `1d` flow, docket, and ribbon are authored HERE.
- **CSP constants:** `INBOX_CSP` + `BOOTSTRAP_CSP` are in `ui/render.py`;
  `DECISION_PAGE_CSP` stays in `server.py`. The D2 relax touches both (§6).
- **Publish path + page contract** (`page_contract.py`, `mcp_server.py`, the
  `server.py` publication endpoint and `_inbox_entries`) were NOT touched by the
  refactor — Stage A applies as written, except its inbox-manifest exposure (§4)
  flows the manifest through `_inbox_entries` → `ui.render_inbox`'s entry data
  and new template slots, not inline `server.py` HTML.

---

## 1. Goal

Today a decision brief is one HTML document holding **both**:
- the *argument* — prose, tables, charts, live simulators; and
- the *verdict widget* — a `<form id="ruling">` of per-axis radios + notes whose
  `<script>` assembles a ruling and `POST`s it to `/ruling`.

The redesign splits these. The **brief keeps only the argument.** The **Arachne
chrome owns capture**, rendering a per-axis *Ruling Docket* beside a reading
pane. The brief declares its axes as structured metadata at publication; the
chrome renders the pills, persists the draft, composes the record, and files the
ruling.

Observable win: rule each axis where you read it (no scroll-to-a-form-at-the-
bottom); progress ("2 of 6 ruled") shows in both the inbox card and the docket;
drafts persist per device.

---

## 2. Load-bearing invariant — the ruling payload does NOT change

The server's storage / wake / cursor layer stays byte-identical:

- `/ruling` still accepts exactly `{issue, markdown, form}` (`server.py:1172–1203`);
  `form` stays an **opaque `dict`**, `markdown` a non-empty string.
- `RulingStore.file(issue, markdown, form)` and the persisted record shape
  (`{sequence, issue, submitted_at, markdown, form, artifacts}`) are untouched.
- The wake channel (`/wait?since=`), the monotonic restart-safe cursor, and the
  MCP consumer surface (`wait_for_ruling → {cursor, ruling}`, `get_ruling`,
  `status`) are untouched.
- Archive derivation (a ruling carrying the page's issue token archives it;
  re-publishing reopens it) is untouched.

**Therefore this is not a server-protocol change; it is a change of *producer*.**
Today the brief's JS builds `form`+`markdown` and calls `/ruling`. After: the
*chrome* builds the identical `form`+`markdown` from the axis manifest and calls
`/ruling`. Everything downstream of `/ruling` is oblivious to the move. This is
the property that de-risks the whole effort.

---

## 3. The axis manifest (new structured metadata)

Derived directly from `decision_480`'s hand-written `AXES` array and its
`<fieldset class="axis">` blocks:

```json
{
  "contract": "v2",
  "issue": "480",
  "title": "Mood Valence and Localized Weather",
  "repo": "NEXUS · Orrery",
  "overall_notes": true,
  "axes": [
    {
      "id": "wscope",
      "label": "Axis 1a · Weather Scope",
      "select": "one",
      "notes": true,
      "options": [
        {"id": "global", "label": "Global (Status Quo Shape)"},
        {"id": "region", "label": "Per-Place / Per-Region"}
      ]
    }
    /* wvocab, wevo, mood, consume, couple … */
  ]
}
```

Per axis: stable `id`, human `label`, `select` (`"one"` today; reserve `"many"`
for future — Axis 3's "All of the above" is expressible as a distinct option
now), an ordered `options` list of `{id, label}`, and `notes: true|false`.

- The two universal escapes — **Defer** and **Discuss — see notes** — are
  contributed by the *chrome* on every axis, not enumerated in the manifest.
- A ruling is **complete** (Send enabled) when every axis has a selection *or*
  is Defer / Discuss.
- `overall_notes` mirrors the brief's `n-overall` textarea.

The manifest gives the chrome everything to (1) render docket pills, (2) validate
completeness, (3) persist a draft, and (4) compose the **same** `form` dict +
`markdown` record the brief produces today — so back-compat is exact.

---

## 4. Where the manifest lives — SIDECAR (recommended; D1)

Follow the existing `.meta/<name>.json` precedent (which already stores the issue
token — `page_contract.py`). `publish_decision` / `page_contract.publish_html`
gain an `axes` argument; the manifest is written to the page's sidecar
atomically alongside the issue. The inbox reads it server-side to render "N/6
axes" progress; the shell reads it to render the docket. The brief HTML never
carries the manifest, so the docket cannot drift from the brief's DOM and the
inbox needs no HTML parsing.

*Alternative (D1):* embed a `<script type="application/json" id="arachne-axes">`
in the brief; the shell extracts it from the same-origin iframe DOM. Single
source file, cannot drift from the page — but couples the docket to reading the
brief DOM and complicates server-side inbox progress.

---

## 5. The page contract — v2 only, hard cutover (D3)

There is no version branching. The page contract **is** the docket contract:

- A published brief embeds NO ruling form, NO `/ruling` reference, NO
  `localStorage`, NO submit script — capture is the chrome's.
- `prepare_html` *requires* a valid axis manifest (§3) and *rejects* any page
  whose body references `/ruling` or `localStorage`. This is the exact inverse
  of today's check, which *requires* both. The old v1 check is **deleted**, not
  gated behind a version flag.
- A page that fails the v2 contract cannot be published (loud error — honor
  "fail loud").

**Migration of the existing pages.** Validation runs at *publish*, not at serve,
so already-published files keep serving as static HTML. Only pages that must
re-publish under the new contract need rewriting:

- **Rewrite to v2:** the active / awaiting briefs — `decision_480` (6 axes),
  `decision_479`, `decision_496`. Strip each one's `<form>` / `<script>` /
  `/ruling` / `localStorage`; author its axis manifest.
- **Leave as frozen archive:** `decision_476` and `decision_plugin_smoke` are
  already ruled. Their docket is read-only history; they render fine in the
  reading pane as-is and their old in-brief forms are inert (nothing re-files an
  archived issue). Rewrite them only for cosmetic uniformity, not correctness.

Chrome responsibilities for every brief:
- render the docket from the manifest;
- persist the in-progress draft to `localStorage` keyed by issue (logic moved
  verbatim from the brief's `save()`/`restore()`);
- compose `markdown` (per-axis "label: choice" + notes + overall) and `form`
  (flat `{[axisId]: choice, [axisId]-notes: text, overall: text}`) — matching
  `decision_480`'s `composeText()` / `formState()`;
- disable **Send Ruling** until every axis is ruled or deferred; file **one**
  ruling per brief (D4);
- `POST {issue, markdown, form}` to `/ruling` — unchanged.

---

## 6. Reading pane & CSP — SAME-ORIGIN IFRAME (recommended; D2)

The shell frames the brief same-origin.
- Decision-page responses (`DECISION_PAGE_CSP`, `server.py`): `frame-ancestors
  'none'` → `frame-ancestors 'self'`.
- Inbox shell response (`INBOX_CSP`, `ui/render.py`): add `frame-src 'self'`.

Briefs remain fully self-contained trusted documents; their explainer simulators
(live softmax bias slider, SVG lane highlights) keep running in their own
document context. **No brief script executes in the shell's origin.**

*Alternative (D2):* app-shell inlining (fetch brief HTML, inject into the pane).
Avoids the iframe but runs brief JS in the shell's origin/CSP — more surface,
must sandbox brief scripts.

**Responsive — two layouts, one shell (D5, decided):**
- **Desktop (≥~1000px):** three panes — list, iframe reading pane, right-hand
  ruling docket (Turn 3). Both seams draggable (list 240–440px, docket 260–420px).
- **Phone (`1d`):** single column, two routes — an inbox screen and a full-screen
  brief (the *same* brief iframe under a fixed app bar: `‹ INBOX · #480 · ‹ ›`,
  44px touch targets). A redesigned Iris flow, **not** today's production inbox.

The reading surface is the same same-origin brief iframe in both — a side pane on
desktop, full-screen under the bar on phone.

**Phone capture — Ruling Ribbon (D6, decided).** The right-column docket has no
room on a 390px screen, so phone capture is the Turn-2 `2b` **Ruling Ribbon**: a
fixed bottom bar showing the axis currently in view (its option pills + Send),
with a row of axis dots to jump between axes, scroll-synced to the reading
content. It is the docket's mobile form — same manifest, same one-ruling Send,
rendered as a horizontal strip instead of a column.

---

## 7. Decisions (all ratified)

| # | Decision | Ruling |
|---|----------|--------|
| D1 | Manifest location | **Sidecar `.meta/*.json`** — beside the issue token; brief HTML stays pure argument |
| D2 | Reading pane | **Same-origin iframe** — decision-page CSP `frame-ancestors 'none'` → `'self'`; inbox adds `frame-src 'self'` |
| D3 | Contract migration | **Hard cutover to v2** — no version branching; old v1 check deleted; rewrite the 3 active pages (§5) |
| D4 | Send granularity | **One ruling per brief** — docket is capture UI; Send files one `{issue, markdown, form}` |
| D5 | Desktop/phone | **Two layouts, one shell** — desktop three-pane (Turn 3); phone `1d` single-column inbox↔full-screen brief |
| D6 | Phone capture | **Ruling Ribbon (`2b` bottom bar)** — fixed bottom bar: in-view axis's pills + Send, axis dots to jump; the docket's mobile form |

---

## 8. Out of scope (later)

- Multi-select axes (`select: "many"`) — leave the schema door open; don't build.
- Any change to the wake / cursor / MCP layer — explicitly untouched.
- Restyling the brief documents themselves — they keep their own CSS.
- Draggable-seam persistence across devices — device-local only, if at all.
