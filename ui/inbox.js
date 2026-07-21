(() => {
  "use strict";

  const DEFERRED_SENTINEL = "deferred";
  const DISCUSS_SENTINEL = "discuss";
  const DRAFT_PREFIX = "arachne:docket:v2:";
  const BRIEF_MESSAGE_SOURCE = "arachne-brief";
  const CHROME_MESSAGE_SOURCE = "arachne-chrome";
  const PENDING_SCROLL_TIMEOUT_MS = 700;
  const LIST_MIN = 240;
  const LIST_MAX = 440;
  const DOCKET_MIN = 260;
  const DOCKET_MAX = 420;

  class ResponseError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ResponseError";
      this.status = status;
    }
  }

  class DefinitelyNotFiledError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "DefinitelyNotFiledError";
      this.status = status;
    }
  }

  class AmbiguousSubmissionError extends Error {
    constructor(message) {
      super(message);
      this.name = "AmbiguousSubmissionError";
    }
  }

  function draftRecord(draft, axis) {
    const record = draft?.axes?.[axis.id];
    if (!record || typeof record !== "object") {
      throw new Error(`axis ${axis.id} has no draft record`);
    }
    return record;
  }

  function axisCompleteForDraft(axis, draft) {
    const record = draftRecord(draft, axis);
    if (record.mode === "choice") {
      return axis.options.some((option) => option.id === record.choice);
    }
    if (record.mode === "deferred") return true;
    if (record.mode === "discuss") {
      return typeof record.note === "string" && Boolean(record.note.trim());
    }
    return false;
  }

  function composeRulingPayload(manifest, draft) {
    if (!manifest || !draft) {
      throw new Error("no axis manifest is loaded");
    }
    const incomplete = manifest.axes.filter(
      (axis) => !axisCompleteForDraft(axis, draft),
    );
    if (incomplete.length) {
      throw new Error(
        `cannot compose while ${incomplete.map((axis) => axis.label).join(", ")} remain incomplete`,
      );
    }

    const form = Object.create(null);
    const lines = [];
    for (const axis of manifest.axes) {
      const record = draftRecord(draft, axis);
      let markdownChoice;
      if (record.mode === "choice") {
        const option = axis.options.find((item) => item.id === record.choice);
        if (!option) throw new Error(`axis ${axis.id} has an unknown option`);
        form[axis.id] = option.id;
        markdownChoice = option.label;
      } else if (record.mode === "deferred") {
        form[axis.id] = DEFERRED_SENTINEL;
        markdownChoice = "Defer [deferred]";
      } else if (record.mode === "discuss") {
        form[axis.id] = DISCUSS_SENTINEL;
        markdownChoice = "Discuss — see notes [discuss]";
      } else {
        throw new Error(`axis ${axis.id} has no ruling state`);
      }

      if (typeof record.note !== "string") {
        throw new Error(`axis ${axis.id} note is not text`);
      }
      const note = record.note.trim();
      const emitNote = axis.notes || record.mode === "discuss";
      if (emitNote) form[`${axis.id}-notes`] = note;
      lines.push(`${axis.label}: ${markdownChoice}`);
      if (emitNote && note) lines.push(`${axis.label} notes: ${note}`);
    }

    if (manifest.overall_notes) {
      if (typeof draft.overall !== "string") {
        throw new Error("overall note is not text");
      }
      const overall = draft.overall.trim();
      form.overall = overall;
      lines.push("", `Overall: ${overall}`);
    }
    return {issue: manifest.issue, markdown: lines.join("\n"), form};
  }

  async function readRulingAcknowledgement(response, submittedIssue) {
    let acknowledgement;
    try {
      acknowledgement = await response.json();
    } catch (error) {
      const bodyKind = response.ok ? "acknowledgement" : "error response";
      throw new AmbiguousSubmissionError(
        `HTTP ${response.status} ${bodyKind} was not valid JSON: ${error.message}`,
      );
    }

    if (!response.ok) {
      const detail =
        acknowledgement && typeof acknowledgement.detail === "string"
          ? acknowledgement.detail.trim()
          : "";
      if (detail) {
        throw new DefinitelyNotFiledError(detail, response.status);
      }
      throw new AmbiguousSubmissionError(
        `the server returned HTTP ${response.status} without a readable error detail`,
      );
    }
    if (
      !acknowledgement ||
      typeof acknowledgement !== "object" ||
      Array.isArray(acknowledgement)
    ) {
      throw new AmbiguousSubmissionError(
        "the ruling acknowledgement was not a JSON object",
      );
    }
    if (String(acknowledgement.issue) !== submittedIssue) {
      throw new AmbiguousSubmissionError(
        `the ruling acknowledgement issue ${String(acknowledgement.issue)} does not match submitted issue ${submittedIssue}`,
      );
    }
    return acknowledgement;
  }

  function submissionFailureKind(error) {
    return error instanceof DefinitelyNotFiledError
      ? "definitely-not-filed"
      : "ambiguous";
  }

  function isValidBriefInViewMessage(data, knownAxisIds) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const keys = Object.keys(data).sort();
    if (
      keys.length !== 3 ||
      keys[0] !== "axis" ||
      keys[1] !== "source" ||
      keys[2] !== "type"
    ) {
      return false;
    }
    if (
      data.source !== BRIEF_MESSAGE_SOURCE ||
      data.type !== "in-view" ||
      typeof data.axis !== "string"
    ) {
      return false;
    }
    if (Array.isArray(knownAxisIds)) return knownAxisIds.includes(data.axis);
    if (knownAxisIds instanceof Set) return knownAxisIds.has(data.axis);
    return false;
  }

  function shouldAcceptInViewReport(pendingScrollAxis, reportedAxis) {
    return pendingScrollAxis === null || pendingScrollAxis === reportedAxis;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze({
      composeRulingPayload,
      isValidBriefInViewMessage,
      readRulingAcknowledgement,
      shouldAcceptInViewReport,
      submissionFailureKind,
    });
  }

  const shell =
    typeof document === "undefined"
      ? null
      : document.querySelector("[data-arachne-shell]");
  if (!shell) return;

  function required(selector) {
    const node = shell.querySelector(selector);
    if (!node) {
      throw new Error(`Arachne shell is missing required element ${selector}`);
    }
    return node;
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  const listPane = required("[data-list-pane]");
  const docketPane = required("[data-docket]");
  const frame = required("[data-reading-frame]");
  const readingEmpty = required("[data-reading-empty]");
  const phoneInboxButton = required("[data-phone-inbox]");
  const phoneReadingContext = required("[data-phone-reading-context]");
  const breadcrumb = required("[data-reading-breadcrumb]");
  const readingStatus = required("[data-reading-status]");
  const previousButton = required("[data-brief-previous]");
  const nextButton = required("[data-brief-next]");
  const expandLink = required("[data-brief-expand]");
  const message = required("[data-docket-message]");
  const axisList = required("[data-axis-list]");
  const meterFill = required("[data-docket-meter-fill]");
  const meterLabel = required("[data-docket-meter-label]");
  const overallField = required("[data-overall-note]");
  const overallTextarea = required("[data-overall-textarea]");
  const sendButton = required("[data-send-ruling]");
  const draftNote = required("[data-draft-note]");
  const ribbon = required("[data-ruling-ribbon]");
  const ribbonMessage = required("[data-ribbon-message]");
  const ribbonBody = required("[data-ribbon-body]");
  const ribbonStepper = required("[data-ribbon-axis-stepper]");
  const ribbonAxisLabel = required("[data-ribbon-axis-label]");
  const ribbonAxisControls = required("[data-ribbon-axis-controls]");
  const ribbonNoteToggle = required("[data-ribbon-note-toggle]");
  const ribbonNotePanel = required("[data-ribbon-note-panel]");
  const ribbonNoteTextarea = required("[data-ribbon-note]");
  const ribbonNoteRequirement = required(
    "[data-ribbon-note-requirement]",
  );
  const ribbonProgress = required("[data-ribbon-progress]");
  const ribbonSendButton = required("[data-ribbon-send]");

  const state = {
    card: null,
    manifest: null,
    draft: null,
    activeAxisId: null,
    loadSequence: 0,
    frameLoadedSequence: 0,
    frameLoadHandler: null,
    submitting: false,
    filed: false,
    storageWarning: "",
    ribbonNoteOpen: false,
    pendingScrollAxis: null,
    pendingScrollStartedAt: 0,
    pendingScrollTimer: null,
  };

  function allCards() {
    return Array.from(shell.querySelectorAll("[data-brief-name]"));
  }

  function pendingCards() {
    return allCards().filter(
      (card) => card.dataset.briefStatus === "awaiting",
    );
  }

  function briefPath(name) {
    return `/${encodeURIComponent(name)}`;
  }

  function axesPath(name) {
    return `/axes/${encodeURIComponent(name)}`;
  }

  function draftKey(issue) {
    return `${DRAFT_PREFIX}${issue}`;
  }

  function showMessage(text, kind = "") {
    message.textContent = text;
    message.className = "docket-message";
    if (kind) message.classList.add(`is-${kind}`);
    ribbonMessage.textContent = text;
    ribbonMessage.className = "ribbon-message";
    if (kind) ribbonMessage.classList.add(`is-${kind}`);
  }

  function clearPendingScroll() {
    if (state.pendingScrollTimer !== null) {
      window.clearTimeout(state.pendingScrollTimer);
    }
    state.pendingScrollAxis = null;
    state.pendingScrollStartedAt = 0;
    state.pendingScrollTimer = null;
  }

  function beginPendingScroll(axisId) {
    clearPendingScroll();
    const startedAt = Date.now();
    state.pendingScrollAxis = axisId;
    state.pendingScrollStartedAt = startedAt;
    state.pendingScrollTimer = window.setTimeout(() => {
      if (
        state.pendingScrollAxis === axisId &&
        state.pendingScrollStartedAt === startedAt
      ) {
        state.pendingScrollAxis = null;
        state.pendingScrollStartedAt = 0;
        state.pendingScrollTimer = null;
      }
    }, PENDING_SCROLL_TIMEOUT_MS);
  }

  function resetRibbon() {
    ribbonBody.hidden = true;
    ribbon.removeAttribute("aria-busy");
    ribbonStepper.replaceChildren();
    ribbonAxisLabel.textContent = "";
    ribbonAxisControls.replaceChildren();
    ribbonNoteToggle.hidden = true;
    ribbonNoteToggle.disabled = true;
    ribbonNoteToggle.setAttribute("aria-expanded", "false");
    ribbonNotePanel.hidden = true;
    ribbonNoteTextarea.value = "";
    ribbonNoteTextarea.required = false;
    ribbonNoteTextarea.setAttribute("aria-invalid", "false");
    ribbonNoteRequirement.hidden = true;
    ribbonProgress.textContent = "0 of 0 ruled";
    ribbonSendButton.disabled = true;
    ribbonSendButton.classList.remove("is-filed");
    ribbonSendButton.textContent = "SEND RULING";
    ribbonSendButton.title = "Select a brief to begin a ruling";
  }

  function resetDocket(text) {
    clearPendingScroll();
    state.manifest = null;
    state.draft = null;
    state.activeAxisId = null;
    state.submitting = false;
    state.filed = false;
    state.storageWarning = "";
    state.ribbonNoteOpen = false;
    axisList.replaceChildren();
    meterFill.style.width = "0%";
    meterLabel.textContent = "0 of 0 ruled";
    overallField.hidden = true;
    overallTextarea.value = "";
    overallTextarea.disabled = false;
    sendButton.disabled = true;
    sendButton.classList.remove("is-filed");
    sendButton.textContent = "SEND RULING";
    draftNote.textContent =
      "draft persists on this device · engage every axis to send";
    resetRibbon();
    if (text === "Loading axis manifest…") {
      ribbon.setAttribute("aria-busy", "true");
    }
    showMessage(text);
  }

  function setToolbar(card) {
    const issue = card.dataset.briefIssue || "";
    const title = card.dataset.briefTitle || "Untitled decision";
    const archived = card.dataset.briefStatus === "archived";
    breadcrumb.textContent = `DECISION #${issue} — ${title}`;
    phoneReadingContext.textContent = `#${issue} · ${title}`;
    readingStatus.textContent = archived ? "ARCHIVED" : "AWAITING";
    readingStatus.hidden = false;
    expandLink.href = briefPath(card.dataset.briefName || "");
    expandLink.setAttribute("aria-disabled", "false");
    updateNavigation();
  }

  function showPhoneBrief() {
    shell.classList.add("is-phone-reading");
  }

  function showPhoneInbox() {
    shell.classList.remove("is-phone-reading");
    state.card?.focus();
  }

  function updateNavigation() {
    const cards = pendingCards();
    const index = state.card ? cards.indexOf(state.card) : -1;
    previousButton.disabled = index <= 0;
    nextButton.disabled = index < 0 || index >= cards.length - 1;
  }

  function knownAxisIds() {
    return state.manifest
      ? state.manifest.axes.map((axis) => axis.id)
      : [];
  }

  function scrollBriefToAxis(axisId) {
    const target = frame.contentWindow;
    if (!target) return;
    beginPendingScroll(axisId);
    target.postMessage(
      {source: CHROME_MESSAGE_SOURCE, type: "scroll-to", axis: axisId},
      "*",
    );
  }

  function requestBriefInView() {
    const target = frame.contentWindow;
    if (!target) return;
    target.postMessage(
      {source: CHROME_MESSAGE_SOURCE, type: "request-in-view"},
      "*",
    );
  }

  function setActiveAxis(axisId, {scrollBrief = false} = {}) {
    if (!knownAxisIds().includes(axisId)) return;
    if (state.card?.dataset.briefStatus === "archived") return;
    if (state.activeAxisId === axisId) {
      if (scrollBrief) scrollBriefToAxis(axisId);
      return;
    }
    state.ribbonNoteOpen = false;
    state.activeAxisId = axisId;
    renderAxes();
    renderRibbon();
    updateCompleteness();
    if (scrollBrief) scrollBriefToAxis(axisId);
  }

  function activateTab(name) {
    for (const tab of shell.querySelectorAll("[data-list-tab]")) {
      const active = tab.dataset.listTab === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    }
    for (const panel of shell.querySelectorAll("[data-list-panel]")) {
      panel.hidden = panel.dataset.listPanel !== name;
    }
  }

  function emptyDraft(manifest) {
    const axes = Object.create(null);
    for (const axis of manifest.axes) {
      axes[axis.id] = {mode: null, choice: null, note: ""};
    }
    return {version: 1, issue: manifest.issue, axes, overall: ""};
  }

  function restoreDraft(manifest) {
    const restored = emptyDraft(manifest);
    const warnings = [];
    let raw;
    try {
      raw = localStorage.getItem(draftKey(manifest.issue));
    } catch (error) {
      state.storageWarning = `Draft storage is unavailable: ${error.message}`;
      return restored;
    }
    if (raw === null) {
      state.storageWarning = "";
      return restored;
    }

    let saved;
    try {
      saved = JSON.parse(raw);
    } catch (error) {
      state.storageWarning =
        `Saved draft for issue ${manifest.issue} is invalid JSON: ${error.message}`;
      return restored;
    }
    if (
      !saved ||
      typeof saved !== "object" ||
      saved.issue !== manifest.issue ||
      !saved.axes ||
      typeof saved.axes !== "object"
    ) {
      state.storageWarning =
        `Saved draft for issue ${manifest.issue} does not match this manifest.`;
      return restored;
    }

    for (const axis of manifest.axes) {
      const candidate = Object.prototype.hasOwnProperty.call(saved.axes, axis.id)
        ? saved.axes[axis.id]
        : null;
      if (!candidate || typeof candidate !== "object") continue;
      const note = typeof candidate.note === "string" ? candidate.note : "";
      if (candidate.mode === "choice") {
        const validChoice = axis.options.some(
          (option) => option.id === candidate.choice,
        );
        if (validChoice) {
          restored.axes[axis.id] = {
            mode: "choice",
            choice: candidate.choice,
            note,
          };
        } else {
          warnings.push(`${axis.label} has a stale option`);
        }
      } else if (candidate.mode === "deferred") {
        restored.axes[axis.id] = {mode: "deferred", choice: null, note};
      } else if (candidate.mode === "discuss") {
        restored.axes[axis.id] = {mode: "discuss", choice: null, note};
      } else if (candidate.mode !== null && candidate.mode !== undefined) {
        warnings.push(`${axis.label} has an unsupported saved state`);
      }
    }
    if (typeof saved.overall === "string") restored.overall = saved.overall;
    state.storageWarning = warnings.length
      ? `Draft restored with warnings: ${warnings.join("; ")}.`
      : "";
    return restored;
  }

  function saveDraft() {
    if (!state.manifest || !state.draft || state.filed) return;
    try {
      localStorage.setItem(
        draftKey(state.manifest.issue),
        JSON.stringify(state.draft),
      );
    } catch (error) {
      state.storageWarning = `Draft could not be saved: ${error.message}`;
      showMessage(state.storageWarning, "error");
      draftNote.textContent = "draft storage failed · this ruling is not persisted";
    }
  }

  function clearDraft(issue) {
    try {
      localStorage.removeItem(draftKey(issue));
      return "";
    } catch (error) {
      return ` The ruling was filed, but its local draft could not be cleared: ${error.message}`;
    }
  }

  function validateManifest(manifest, card) {
    if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
      throw new Error("axis manifest is not a JSON object");
    }
    if (manifest.contract !== "v2") {
      throw new Error(`axis manifest contract is ${String(manifest.contract)}, not v2`);
    }
    if (typeof manifest.issue !== "string" || !manifest.issue.trim()) {
      throw new Error("axis manifest has no issue token");
    }
    if (manifest.issue !== card.dataset.briefIssue) {
      throw new Error(
        `axis manifest issue ${manifest.issue} does not match inbox issue ${card.dataset.briefIssue}`,
      );
    }
    if (!Array.isArray(manifest.axes) || manifest.axes.length === 0) {
      throw new Error("axis manifest contains no axes");
    }
    const formKeys = new Map();
    const claimFormKey = (key, owner) => {
      if (formKeys.has(key)) {
        throw new Error(
          `axis manifest produces colliding form key ${key} for ${formKeys.get(key)} and ${owner}`,
        );
      }
      formKeys.set(key, owner);
    };
    for (const axis of manifest.axes) {
      if (
        !axis ||
        typeof axis.id !== "string" ||
        typeof axis.label !== "string" ||
        axis.select !== "one" ||
        typeof axis.notes !== "boolean" ||
        !Array.isArray(axis.options) ||
        axis.options.length === 0
      ) {
        throw new Error("axis manifest contains an invalid single-select axis");
      }
      for (const option of axis.options) {
        if (
          !option ||
          typeof option.id !== "string" ||
          typeof option.label !== "string"
        ) {
          throw new Error(`axis ${axis.id} contains an invalid option`);
        }
        if (
          option.id === DEFERRED_SENTINEL ||
          option.id === DISCUSS_SENTINEL
        ) {
          throw new Error(
            `axis ${axis.id} option ${option.id} collides with a universal docket escape`,
          );
        }
      }
      claimFormKey(axis.id, `axis ${axis.id}`);
      // Discuss can add a note even when the ordinary axis does not expose one.
      claimFormKey(`${axis.id}-notes`, `notes for axis ${axis.id}`);
    }
    if (manifest.overall_notes) claimFormKey("overall", "overall notes");
  }

  async function fetchManifest(name) {
    const response = await fetch(axesPath(name), {
      headers: {Accept: "application/json"},
      credentials: "same-origin",
    });
    if (!response.ok) {
      let detail = "";
      try {
        const problem = await response.json();
        detail = typeof problem.detail === "string" ? `: ${problem.detail}` : "";
      } catch (_error) {
        detail = "";
      }
      throw new ResponseError(
        `axis manifest request failed (${response.status})${detail}`,
        response.status,
      );
    }
    try {
      return await response.json();
    } catch (error) {
      throw new Error(`axis manifest response is not valid JSON: ${error.message}`);
    }
  }

  async function selectBrief(card) {
    if (state.card === card) return;
    showPhoneBrief();
    const selection = ++state.loadSequence;
    state.card = card;
    for (const candidate of allCards()) {
      candidate.setAttribute("aria-current", String(candidate === card));
    }

    setToolbar(card);
    if (state.frameLoadHandler) {
      frame.removeEventListener("load", state.frameLoadHandler);
    }
    const frameLoadHandler = () => {
      if (state.frameLoadHandler === frameLoadHandler) {
        state.frameLoadHandler = null;
      }
      if (selection !== state.loadSequence) return;
      state.frameLoadedSequence = selection;
      if (state.manifest) requestBriefInView();
    };
    state.frameLoadHandler = frameLoadHandler;
    frame.addEventListener("load", frameLoadHandler, {once: true});
    frame.src = briefPath(card.dataset.briefName || "");
    frame.hidden = false;
    readingEmpty.hidden = true;
    resetDocket("Loading axis manifest…");

    try {
      const manifest = await fetchManifest(card.dataset.briefName || "");
      if (selection !== state.loadSequence) return;
      validateManifest(manifest, card);
      state.manifest = manifest;
      card.dataset.axisCount = String(manifest.axes.length);

      if (card.dataset.briefStatus === "archived") {
        state.draft = emptyDraft(manifest);
        renderArchivedDocket(
          manifest,
          `Ruling ${card.dataset.rulingSequence || "filed"} already acknowledges this brief.`,
          false,
        );
        requestBriefInView();
        return;
      }

      state.draft = restoreDraft(manifest);
      state.activeAxisId =
        manifest.axes.find((axis) => !axisComplete(axis))?.id || manifest.axes[0].id;
      state.filed = false;
      renderDocket();
      requestBriefInView();
    } catch (error) {
      if (selection !== state.loadSequence) return;
      if (
        card.dataset.briefStatus === "archived" &&
        error instanceof ResponseError &&
        error.status === 404
      ) {
        renderArchivedDocket(
          null,
          `Ruling ${card.dataset.rulingSequence || "filed"} is archived; this legacy brief has no v2 axis manifest.`,
          false,
        );
        return;
      }
      resetDocket("");
      showMessage(
        `Could not load ${axesPath(card.dataset.briefName || "")}: ${error.message}`,
        "error",
      );
      console.error(error);
    }
  }

  function recordFor(axis) {
    return draftRecord(state.draft, axis);
  }

  function axisComplete(axis) {
    if (!state.draft) return false;
    return axisCompleteForDraft(axis, state.draft);
  }

  function engagedCount() {
    if (!state.manifest || !state.draft) return 0;
    return state.manifest.axes.filter(axisComplete).length;
  }

  function displayChoice(axis, record) {
    if (record.mode === "deferred") return "Defer";
    if (record.mode === "discuss") return "Discuss — see notes";
    if (record.mode === "choice") {
      return (
        axis.options.find((option) => option.id === record.choice)?.label ||
        "Unknown option"
      );
    }
    return "unruled — select to edit";
  }

  function activeAxis() {
    if (!state.manifest) return null;
    return (
      state.manifest.axes.find((axis) => axis.id === state.activeAxisId) ||
      state.manifest.axes[0] ||
      null
    );
  }

  function chooseRibbonState(axis, mode, choice = null) {
    if (state.submitting || state.filed) return;
    const record = recordFor(axis);
    state.activeAxisId = axis.id;
    record.mode = mode;
    record.choice = mode === "choice" ? choice : null;
    state.ribbonNoteOpen =
      mode === "discuss" ? true : state.ribbonNoteOpen && axis.notes;
    saveDraft();
    renderAxes();
    renderRibbon();
    updateCompleteness();
    if (mode === "discuss") {
      requestAnimationFrame(() => ribbonNoteTextarea.focus());
    }
  }

  function ribbonPill(axis, text, className, selected, mode, choice = null) {
    const pill = make("button", `ribbon-option-pill ${className}`.trim(), text);
    pill.type = "button";
    pill.disabled = state.submitting || state.filed;
    pill.classList.toggle("is-selected", selected);
    pill.setAttribute("aria-pressed", String(selected));
    pill.addEventListener("click", () => {
      chooseRibbonState(axis, mode, choice);
    });
    return pill;
  }

  function renderRibbon() {
    if (!state.manifest || !state.draft) {
      ribbonBody.hidden = true;
      return;
    }
    ribbon.removeAttribute("aria-busy");
    ribbonBody.hidden = false;
    ribbonStepper.replaceChildren();
    for (const axis of state.manifest.axes) {
      const complete = axisComplete(axis);
      const inView = state.activeAxisId === axis.id;
      const dot = make("button", "ribbon-axis-dot");
      dot.type = "button";
      dot.classList.add(
        inView ? "is-active" : complete ? "is-ruled" : "is-unruled",
      );
      dot.setAttribute("aria-pressed", String(inView));
      dot.setAttribute(
        "aria-label",
        `${axis.label}: ${inView ? "in view" : complete ? "ruled" : "unruled"}`,
      );
      dot.title = `${axis.label} — ${inView ? "in view" : complete ? "ruled" : "unruled"}`;
      dot.addEventListener("click", () => {
        setActiveAxis(axis.id, {scrollBrief: true});
      });
      ribbonStepper.append(dot);
    }

    const axis = activeAxis();
    if (!axis) {
      ribbonAxisLabel.textContent = "";
      ribbonAxisControls.replaceChildren();
      return;
    }
    const record = recordFor(axis);
    ribbonAxisLabel.textContent = axis.label;
    ribbonAxisControls.setAttribute("aria-label", `${axis.label} choices`);
    ribbonAxisControls.replaceChildren();
    for (const option of axis.options) {
      ribbonAxisControls.append(
        ribbonPill(
          axis,
          option.label,
          "",
          record.mode === "choice" && record.choice === option.id,
          "choice",
          option.id,
        ),
      );
    }
    ribbonAxisControls.append(
      ribbonPill(
        axis,
        "Defer",
        "is-defer",
        record.mode === "deferred",
        "deferred",
      ),
      ribbonPill(
        axis,
        "Discuss",
        "is-discuss",
        record.mode === "discuss",
        "discuss",
      ),
    );

    const noteAvailable = axis.notes || record.mode === "discuss";
    if (!noteAvailable) state.ribbonNoteOpen = false;
    ribbonNoteToggle.hidden = !noteAvailable;
    ribbonNoteToggle.disabled = state.submitting || state.filed;
    ribbonNoteToggle.setAttribute(
      "aria-expanded",
      String(noteAvailable && state.ribbonNoteOpen),
    );
    ribbonNoteToggle.setAttribute("aria-label", `Show note for ${axis.label}`);
    ribbonNotePanel.hidden = !noteAvailable || !state.ribbonNoteOpen;
    ribbonNoteTextarea.disabled = state.submitting || state.filed;
    ribbonNoteTextarea.value = record.note;
    ribbonNoteTextarea.placeholder =
      record.mode === "discuss"
        ? "Required: what needs discussion?"
        : "Add a note for this axis…";
    ribbonNoteTextarea.setAttribute("aria-label", `${axis.label} note`);
    ribbonNoteTextarea.required = record.mode === "discuss";
    const noteMissing = record.mode === "discuss" && !record.note.trim();
    ribbonNoteTextarea.setAttribute("aria-invalid", String(noteMissing));
    ribbonNoteRequirement.hidden = !noteMissing;
  }

  function renderArchivedRibbon(manifest) {
    resetRibbon();
    ribbonBody.hidden = false;
    ribbonAxisLabel.textContent = "ARCHIVED RULING";
    ribbonNoteToggle.hidden = true;
    const total = manifest ? manifest.axes.length : 0;
    if (manifest) {
      for (const axis of manifest.axes) {
        const dot = make("button", "ribbon-axis-dot is-ruled");
        dot.type = "button";
        dot.disabled = true;
        dot.setAttribute("aria-label", `${axis.label}: ruled`);
        dot.title = `${axis.label} — ruled`;
        ribbonStepper.append(dot);
      }
    }
    ribbonProgress.textContent = total
      ? `${total} of ${total} ruled`
      : "ruling filed";
    ribbonSendButton.disabled = true;
    ribbonSendButton.classList.add("is-filed");
    ribbonSendButton.textContent = "FILED";
    ribbonSendButton.title = "This brief already has a ruling";
  }

  function renderDocket() {
    if (!state.manifest || !state.draft) return;
    const pending = state.card?.dataset.rulingSubmissionPending === "true";
    const uncertain = state.card?.dataset.rulingSubmissionUncertain || "";
    const rejected = state.card?.dataset.rulingSubmissionError || "";
    if (uncertain) {
      state.filed = true;
      state.submitting = false;
    } else if (pending) {
      state.submitting = true;
    }
    overallField.hidden = !state.manifest.overall_notes;
    overallTextarea.value = state.draft.overall;
    sendButton.classList.remove("is-filed");
    sendButton.textContent = "SEND RULING";
    draftNote.textContent =
      "draft persists on this device · engage every axis to send";
    if (uncertain) {
      showMessage(
        `Ruling submission status is UNCERTAIN: ${uncertain}. Reload and check the Archive before resubmitting.`,
        "error",
      );
    } else if (rejected) {
      showMessage(`Ruling was not filed: ${rejected}`, "error");
    } else if (pending) {
      showMessage("Filing one ruling for this brief…");
    } else if (state.storageWarning) {
      showMessage(state.storageWarning, "error");
    } else {
      showMessage("");
    }
    renderAxes();
    renderRibbon();
    updateCompleteness();
  }

  function renderAxes() {
    axisList.replaceChildren();
    for (const axis of state.manifest.axes) {
      axisList.append(renderAxis(axis));
    }
  }

  function renderAxis(axis) {
    const record = recordFor(axis);
    const complete = axisComplete(axis);
    const editing = state.activeAxisId === axis.id && !state.filed;
    const card = make("article", "axis-card");
    card.dataset.axisId = axis.id;
    card.classList.add(
      editing ? "is-editing" : complete ? "is-ruled" : "is-unruled",
    );

    const heading = make("button", "axis-heading");
    heading.type = "button";
    heading.setAttribute("aria-expanded", String(editing));
    const titleRow = make("span", "axis-title-row");
    if (complete && !editing) {
      titleRow.append(make("span", "axis-check", "✓"));
    }
    titleRow.append(make("span", "axis-label", axis.label));
    if (editing) {
      titleRow.append(make("span", "in-view-tag", "IN VIEW"));
    }
    heading.append(titleRow);

    if (!editing) {
      const summary = make("span", "axis-summary", displayChoice(axis, record));
      if (!complete) summary.classList.add("is-muted");
      if (record.note.trim()) {
        summary.append(make("span", "note-count", " · 1 note"));
      }
      heading.append(summary);
    }
    heading.addEventListener("click", () => {
      setActiveAxis(axis.id);
    });
    card.append(heading);

    if (editing) {
      const controls = make("div", "axis-controls");
      const pills = make("div", "option-pills");
      pills.setAttribute("role", "group");
      pills.setAttribute("aria-label", `${axis.label} choices`);

      for (const option of axis.options) {
        const pill = make("button", "option-pill", option.label);
        pill.type = "button";
        const selected =
          record.mode === "choice" && record.choice === option.id;
        pill.classList.toggle("is-selected", selected);
        pill.setAttribute("aria-pressed", String(selected));
        pill.addEventListener("click", () => {
          record.mode = "choice";
          record.choice = option.id;
          state.ribbonNoteOpen = state.ribbonNoteOpen && axis.notes;
          saveDraft();
          renderAxes();
          renderRibbon();
          updateCompleteness();
        });
        pills.append(pill);
      }

      const defer = make("button", "option-pill", "Defer");
      defer.type = "button";
      defer.classList.toggle("is-selected", record.mode === "deferred");
      defer.setAttribute("aria-pressed", String(record.mode === "deferred"));
      defer.addEventListener("click", () => {
        record.mode = "deferred";
        record.choice = null;
        state.ribbonNoteOpen = state.ribbonNoteOpen && axis.notes;
        saveDraft();
        renderAxes();
        renderRibbon();
        updateCompleteness();
      });
      pills.append(defer);

      const discuss = make("button", "option-pill is-discuss", "Discuss");
      discuss.type = "button";
      discuss.classList.toggle("is-selected", record.mode === "discuss");
      discuss.setAttribute("aria-pressed", String(record.mode === "discuss"));
      discuss.addEventListener("click", () => {
        record.mode = "discuss";
        record.choice = null;
        state.ribbonNoteOpen = true;
        saveDraft();
        renderAxes();
        renderRibbon();
        updateCompleteness();
        requestAnimationFrame(() => {
          axisList
            .querySelector(`[data-axis-id="${CSS.escape(axis.id)}"] textarea`)
            ?.focus();
        });
      });
      pills.append(discuss);
      controls.append(pills);

      if (axis.notes || record.mode === "discuss") {
        const note = make("textarea", "axis-note");
        note.rows = 3;
        note.value = record.note;
        note.placeholder =
          record.mode === "discuss"
            ? "Required: what needs discussion?"
            : "Add a note for this axis…";
        note.setAttribute("aria-label", `${axis.label} note`);
        note.required = record.mode === "discuss";
        note.setAttribute(
          "aria-invalid",
          String(record.mode === "discuss" && !record.note.trim()),
        );
        const requirement = make(
          "p",
          "discuss-requirement",
          "Discuss requires a note before this axis is complete.",
        );
        requirement.hidden =
          record.mode !== "discuss" || Boolean(record.note.trim());
        note.addEventListener("input", () => {
          record.note = note.value;
          const missing = record.mode === "discuss" && !record.note.trim();
          note.setAttribute("aria-invalid", String(missing));
          requirement.hidden = !missing;
          saveDraft();
          renderRibbon();
          updateCompleteness();
        });
        controls.append(note, requirement);
      }
      card.append(controls);
    }
    return card;
  }

  function updateCardProgress(count, total) {
    if (!state.card) return;
    state.card.dataset.axisCount = String(total);
    const fill = state.card.querySelector(".brief-progress-track > span");
    const label = state.card.querySelector(".brief-progress-label");
    if (fill) fill.style.width = `${total ? (count / total) * 100 : 0}%`;
    if (label) label.textContent = `${count}/${total} axes`;
  }

  function updateCompleteness() {
    if (!state.manifest || !state.draft) return;
    const total = state.manifest.axes.length;
    const count = engagedCount();
    const uncertain = Boolean(
      state.card?.dataset.rulingSubmissionUncertain,
    );
    meterFill.style.width = `${(count / total) * 100}%`;
    meterLabel.textContent = `${count} of ${total} ruled`;
    ribbonProgress.textContent = `${count} of ${total} ruled`;
    updateCardProgress(count, total);
    const incomplete = total - count;
    sendButton.disabled =
      incomplete !== 0 || state.submitting || state.filed || uncertain;
    ribbonSendButton.disabled = sendButton.disabled;
    const captureLocked = state.submitting || state.filed;
    for (const control of axisList.querySelectorAll(".option-pill, .axis-note")) {
      control.disabled = captureLocked;
    }
    overallTextarea.disabled = captureLocked;
    for (const control of ribbonAxisControls.querySelectorAll("button")) {
      control.disabled = captureLocked;
    }
    ribbonNoteTextarea.disabled = captureLocked;
    ribbonNoteToggle.disabled = captureLocked;
    ribbon.removeAttribute("aria-busy");
    if (uncertain) {
      sendButton.textContent = "STATUS UNCERTAIN";
      sendButton.title = "Reload and check the Archive before resubmitting";
      ribbonSendButton.textContent = "UNCERTAIN";
      ribbonSendButton.title =
        "Reload and check the Archive before resubmitting";
      draftNote.textContent = "reload · check archive before any resubmission";
    } else if (state.submitting) {
      sendButton.textContent = "FILING…";
      sendButton.title = "Ruling submission is in progress";
      ribbon.setAttribute("aria-busy", "true");
      ribbonSendButton.textContent = "FILING…";
      ribbonSendButton.title = "Ruling submission is in progress";
    } else {
      sendButton.textContent = "SEND RULING";
      sendButton.title = incomplete
        ? `${incomplete} ${incomplete === 1 ? "axis is" : "axes are"} incomplete`
        : "File one ruling for this brief";
      ribbonSendButton.textContent = "SEND RULING";
      ribbonSendButton.title = sendButton.title;
    }
  }

  function renderArchivedDocket(manifest, detail, useCurrentChoices) {
    state.filed = true;
    state.submitting = false;
    state.activeAxisId = null;
    overallField.hidden = true;
    axisList.replaceChildren();
    const total = manifest ? manifest.axes.length : 0;
    meterFill.style.width = total ? "100%" : "0%";
    meterLabel.textContent = total ? `${total} of ${total} ruled` : "ruling filed";
    showMessage(detail, "acknowledged");

    if (manifest) {
      for (const axis of manifest.axes) {
        const card = make("article", "axis-card is-ruled is-archived");
        const heading = make("div", "axis-heading");
        const row = make("span", "axis-title-row");
        row.append(
          make("span", "axis-check", "✓"),
          make("span", "axis-label", axis.label),
        );
        heading.append(row);
        const record = state.draft?.axes?.[axis.id];
        const summaryText =
          useCurrentChoices && record
            ? displayChoice(axis, record)
            : `Filed in ruling ${state.card?.dataset.rulingSequence || "archive"}`;
        const summary = make("span", "axis-summary", summaryText);
        if (useCurrentChoices && record?.note?.trim()) {
          summary.append(make("span", "note-count", " · 1 note"));
        }
        heading.append(summary);
        card.append(heading);
        axisList.append(card);
      }
    }

    sendButton.disabled = true;
    sendButton.classList.add("is-filed");
    sendButton.textContent = "RULING FILED";
    sendButton.title = "This brief already has a ruling";
    draftNote.textContent = "one ruling per brief · draft cleared after filing";
    renderArchivedRibbon(manifest);
  }

  function composeRuling() {
    return composeRulingPayload(state.manifest, state.draft);
  }

  function setListCount(name, value) {
    const target = shell.querySelector(`[data-list-count="${name}"]`);
    if (target) target.textContent = String(value);
  }

  function listCount(name) {
    const target = shell.querySelector(`[data-list-count="${name}"]`);
    return Number.parseInt(target?.textContent || "0", 10) || 0;
  }

  function ensureListPlaceholder(name, text) {
    const list = shell.querySelector(`[data-brief-list="${name}"]`);
    if (!list) return;
    for (const placeholder of list.querySelectorAll(".empty")) {
      placeholder.remove();
    }
    if (!list.querySelector("[data-brief-item]")) {
      list.append(make("li", "empty", text));
    }
  }

  function archiveCurrentCard(card, acknowledgement, keepSelected) {
    if (!card) throw new Error("cannot archive a missing submitted card");
    const wasAwaiting = card.dataset.briefStatus === "awaiting";
    const sequence = String(acknowledgement.sequence || "filed");
    card.dataset.briefStatus = "archived";
    card.dataset.rulingSequence = sequence;
    let suffix = card.querySelector(".brief-ruling-suffix");
    if (!suffix) {
      suffix = make("span", "brief-ruling-suffix");
      card.querySelector(".brief-title-row")?.append(suffix);
    }
    suffix.textContent = `ruling ${sequence}`;

    const item = card.closest("[data-brief-item]");
    const archive = shell.querySelector('[data-brief-list="archive"]');
    archive?.querySelector(".empty")?.remove();
    if (item && archive) archive.prepend(item);
    if (wasAwaiting) {
      setListCount("awaiting", Math.max(0, listCount("awaiting") - 1));
      setListCount("archive", listCount("archive") + 1);
      ensureListPlaceholder(
        "awaiting",
        "The loom is quiet — no briefs await your ruling.",
      );
    }
    if (keepSelected) {
      activateTab("archive");
      setToolbar(card);
    }
    updateNavigation();
  }

  async function fileRuling() {
    if (state.submitting || state.filed) return;
    let payload;
    try {
      payload = composeRuling();
    } catch (error) {
      showMessage(`Ruling is not ready: ${error.message}`, "error");
      return;
    }

    const submittedCard = state.card;
    const submittedManifest = state.manifest;
    const submittedDraft = state.draft;
    const submittedIssue = payload.issue;
    if (!submittedCard || !submittedManifest || !submittedDraft) {
      showMessage(
        "Ruling is not ready: the submitted card lost its loaded docket state",
        "error",
      );
      return;
    }

    delete submittedCard.dataset.rulingSubmissionError;
    delete submittedCard.dataset.rulingSubmissionUncertain;
    submittedCard.dataset.rulingSubmissionPending = "true";
    state.submitting = true;
    updateCompleteness();
    showMessage("Filing one ruling for this brief…");
    let acknowledgement;
    try {
      const response = await fetch("/ruling", {
        method: "POST",
        headers: {"Content-Type": "application/json", Accept: "application/json"},
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      acknowledgement = await readRulingAcknowledgement(
        response,
        submittedIssue,
      );
    } catch (error) {
      delete submittedCard.dataset.rulingSubmissionPending;
      const stillSelected = state.card === submittedCard;
      if (submissionFailureKind(error) === "definitely-not-filed") {
        submittedCard.dataset.rulingSubmissionError = error.message;
        if (stillSelected) {
          state.submitting = false;
          state.filed = false;
          showMessage(`Ruling was not filed: ${error.message}`, "error");
          updateCompleteness();
        }
      } else {
        submittedCard.dataset.rulingSubmissionUncertain = error.message;
        delete submittedCard.dataset.rulingSubmissionError;
        if (stillSelected) {
          state.submitting = false;
          state.filed = true;
          showMessage(
            `Ruling submission status is UNCERTAIN: ${error.message}. Reload and check the Archive before resubmitting.`,
            "error",
          );
          updateCompleteness();
        }
      }
      console.error(error);
      return;
    }

    delete submittedCard.dataset.rulingSubmissionPending;
    delete submittedCard.dataset.rulingSubmissionError;
    delete submittedCard.dataset.rulingSubmissionUncertain;
    const stillSelected = state.card === submittedCard;
    const clearWarning = clearDraft(submittedIssue);
    archiveCurrentCard(submittedCard, acknowledgement, stillSelected);
    if (stillSelected) {
      state.manifest = submittedManifest;
      state.draft = submittedDraft;
      state.filed = true;
      state.submitting = false;
      renderArchivedDocket(
        submittedManifest,
        `Ruling ${acknowledgement.sequence || "filed"} was filed and this brief is now archived.${clearWarning}`,
        true,
      );
    } else if (clearWarning) {
      console.error(clearWarning.trim());
    }
  }

  function resizeFromKeyboard(resizer, direction) {
    const isList = resizer.dataset.resizer === "list";
    const target = isList ? listPane : docketPane;
    const minimum = isList ? LIST_MIN : DOCKET_MIN;
    const maximum = isList ? LIST_MAX : DOCKET_MAX;
    const current = target.getBoundingClientRect().width;
    let next = current;
    if (direction === "minimum") next = minimum;
    if (direction === "maximum") next = maximum;
    if (direction === "decrease") next = current - 10;
    if (direction === "increase") next = current + 10;
    next = clamp(next, minimum, maximum);
    shell.style.setProperty(
      isList ? "--list-width" : "--docket-width",
      `${next}px`,
    );
    resizer.setAttribute("aria-valuenow", String(Math.round(next)));
  }

  function wireResizer(resizer) {
    const isList = resizer.dataset.resizer === "list";
    const target = isList ? listPane : docketPane;
    const minimum = isList ? LIST_MIN : DOCKET_MIN;
    const maximum = isList ? LIST_MAX : DOCKET_MAX;

    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = target.getBoundingClientRect().width;
      resizer.setPointerCapture(event.pointerId);
      resizer.classList.add("is-active");
      shell.classList.add("is-resizing");

      const move = (moveEvent) => {
        const delta = moveEvent.clientX - startX;
        const width = clamp(
          startWidth + (isList ? delta : -delta),
          minimum,
          maximum,
        );
        shell.style.setProperty(
          isList ? "--list-width" : "--docket-width",
          `${width}px`,
        );
        resizer.setAttribute("aria-valuenow", String(Math.round(width)));
      };
      const stop = () => {
        if (resizer.hasPointerCapture(event.pointerId)) {
          resizer.releasePointerCapture(event.pointerId);
        }
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
        resizer.classList.remove("is-active");
        shell.classList.remove("is-resizing");
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop, {once: true});
      window.addEventListener("pointercancel", stop, {once: true});
    });

    resizer.addEventListener("keydown", (event) => {
      let direction = null;
      if (event.key === "Home") direction = "minimum";
      if (event.key === "End") direction = "maximum";
      if (event.key === "ArrowLeft") {
        direction = isList ? "decrease" : "increase";
      }
      if (event.key === "ArrowRight") {
        direction = isList ? "increase" : "decrease";
      }
      if (!direction) return;
      event.preventDefault();
      resizeFromKeyboard(resizer, direction);
    });
  }

  for (const tab of shell.querySelectorAll("[data-list-tab]")) {
    tab.addEventListener("click", () => activateTab(tab.dataset.listTab));
  }
  for (const card of allCards()) {
    card.addEventListener("click", () => {
      showPhoneBrief();
      void selectBrief(card);
    });
  }
  for (const resizer of shell.querySelectorAll("[data-resizer]")) {
    wireResizer(resizer);
  }

  previousButton.addEventListener("click", () => {
    const cards = pendingCards();
    const index = cards.indexOf(state.card);
    if (index > 0) void selectBrief(cards[index - 1]);
  });
  nextButton.addEventListener("click", () => {
    const cards = pendingCards();
    const index = cards.indexOf(state.card);
    if (index >= 0 && index < cards.length - 1) {
      void selectBrief(cards[index + 1]);
    }
  });
  expandLink.addEventListener("click", (event) => {
    if (expandLink.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
    }
  });
  phoneInboxButton.addEventListener("click", showPhoneInbox);
  ribbonNoteToggle.addEventListener("click", () => {
    const axis = activeAxis();
    if (!axis) return;
    const record = recordFor(axis);
    if (!axis.notes && record.mode !== "discuss") return;
    state.ribbonNoteOpen = !state.ribbonNoteOpen;
    renderRibbon();
    if (state.ribbonNoteOpen) {
      requestAnimationFrame(() => ribbonNoteTextarea.focus());
    }
  });
  ribbonNoteTextarea.addEventListener("input", () => {
    const axis = activeAxis();
    if (!axis || !state.draft || state.submitting || state.filed) return;
    const record = recordFor(axis);
    record.note = ribbonNoteTextarea.value;
    const missing = record.mode === "discuss" && !record.note.trim();
    ribbonNoteTextarea.setAttribute("aria-invalid", String(missing));
    ribbonNoteRequirement.hidden = !missing;
    saveDraft();
    renderAxes();
    updateCompleteness();
  });
  overallTextarea.addEventListener("input", () => {
    if (!state.draft || state.submitting || state.filed) return;
    state.draft.overall = overallTextarea.value;
    saveDraft();
  });
  sendButton.addEventListener("click", () => void fileRuling());
  ribbonSendButton.addEventListener("click", () => void fileRuling());
  window.addEventListener("message", (event) => {
    // contentWindow is a persistent WindowProxy across iframe navigations.
    if (event.source !== frame.contentWindow) return;
    if (state.frameLoadedSequence !== state.loadSequence) return;
    if (!isValidBriefInViewMessage(event.data, knownAxisIds())) return;
    if (
      !shouldAcceptInViewReport(
        state.pendingScrollAxis,
        event.data.axis,
      )
    ) {
      return;
    }
    if (state.pendingScrollAxis === event.data.axis) clearPendingScroll();
    setActiveAxis(event.data.axis);
  });

  updateNavigation();
  const initial = pendingCards()[0];
  const startsOnPhone =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 760px)").matches;
  if (initial && !startsOnPhone) {
    void selectBrief(initial);
  }
})()
