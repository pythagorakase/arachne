(() => {
  "use strict";

  const DRAFT_PREFIX = "arachne:draft:v3:";
  const BRIEF_MESSAGE_SOURCE = "arachne-brief";
  const CHROME_MESSAGE_SOURCE = "arachne-chrome";
  const COLLECT_TIMEOUT_MS = 1500;
  const PENDING_SCROLL_TIMEOUT_MS = 700;
  const LIST_MIN = 240;
  const LIST_MAX = 440;
  const NAV_MIN = 260;
  const NAV_MAX = 420;

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

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function hasExactKeys(value, expected) {
    if (!isPlainObject(value)) return false;
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return (
      actual.length === wanted.length &&
      wanted.every((key, index) => actual[index] === key)
    );
  }

  function isValidBriefCaptureMessage(data) {
    if (
      !hasExactKeys(data, [
        "allAnswered",
        "form",
        "issue",
        "markdown",
        "parts",
        "source",
        "type",
      ]) ||
      data.source !== BRIEF_MESSAGE_SOURCE ||
      data.type !== "capture" ||
      typeof data.issue !== "string" ||
      typeof data.markdown !== "string" ||
      typeof data.allAnswered !== "boolean" ||
      !Array.isArray(data.parts) ||
      !isPlainObject(data.form)
    ) {
      return false;
    }
    const ids = new Set();
    for (const part of data.parts) {
      if (
        !hasExactKeys(part, ["answered", "id", "label"]) ||
        typeof part.id !== "string" ||
        !part.id ||
        typeof part.label !== "string" ||
        typeof part.answered !== "boolean" ||
        ids.has(part.id)
      ) {
        return false;
      }
      ids.add(part.id);
    }
    return (
      data.allAnswered ===
      (data.parts.length > 0 && data.parts.every((part) => part.answered))
    );
  }

  function isValidBriefInViewMessage(data, knownPartIds) {
    if (
      !hasExactKeys(data, ["axis", "source", "type"]) ||
      data.source !== BRIEF_MESSAGE_SOURCE ||
      data.type !== "in-view" ||
      typeof data.axis !== "string"
    ) {
      return false;
    }
    if (Array.isArray(knownPartIds)) return knownPartIds.includes(data.axis);
    if (knownPartIds instanceof Set) return knownPartIds.has(data.axis);
    return false;
  }

  function isValidBriefRulingMessage(data) {
    return (
      hasExactKeys(data, [
        "allAnswered",
        "form",
        "markdown",
        "source",
        "token",
        "type",
      ]) &&
      data.source === BRIEF_MESSAGE_SOURCE &&
      data.type === "ruling" &&
      typeof data.token === "string" &&
      data.token.length > 0 &&
      typeof data.markdown === "string" &&
      typeof data.allAnswered === "boolean" &&
      isPlainObject(data.form)
    );
  }

  function makeCollectMessage(token) {
    if (typeof token !== "string" || !token) {
      throw new TypeError("collect token must be a non-empty string");
    }
    return {source: CHROME_MESSAGE_SOURCE, type: "collect", token};
  }

  function rulingMatchesPendingToken(ruling, pendingToken) {
    return (
      isValidBriefRulingMessage(ruling) &&
      typeof pendingToken === "string" &&
      ruling.token === pendingToken
    );
  }

  function formShapeFingerprint(form) {
    if (!isPlainObject(form)) {
      throw new TypeError("draft form must be a plain object");
    }
    return JSON.stringify(Object.keys(form).sort());
  }

  function makeDraftRecord(form) {
    return {fingerprint: formShapeFingerprint(form), form};
  }

  function isValidDraftRecord(value) {
    return (
      hasExactKeys(value, ["fingerprint", "form"]) &&
      typeof value.fingerprint === "string" &&
      isPlainObject(value.form)
    );
  }

  function draftMatchesForm(draft, form) {
    return (
      isValidDraftRecord(draft) &&
      draft.fingerprint === formShapeFingerprint(form)
    );
  }

  function isMessageFromCurrentBrief(
    eventSource,
    frameWindow,
    documentVouched,
    frameLoadedSequence,
    loadSequence,
  ) {
    return (
      eventSource === frameWindow &&
      documentVouched &&
      frameLoadedSequence === loadSequence
    );
  }

  function shouldAcceptInViewReport(pendingScrollPart, reportedPart) {
    return pendingScrollPart === null || pendingScrollPart === reportedPart;
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
    if (!isPlainObject(acknowledgement)) {
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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze({
      draftMatchesForm,
      formShapeFingerprint,
      isValidBriefCaptureMessage,
      isValidBriefInViewMessage,
      isValidBriefRulingMessage,
      isMessageFromCurrentBrief,
      makeCollectMessage,
      makeDraftRecord,
      readRulingAcknowledgement,
      rulingMatchesPendingToken,
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
  const navPane = required("[data-ruling-nav]");
  const frame = required("[data-reading-frame]");
  const readingEmpty = required("[data-reading-empty]");
  const phoneInboxButton = required("[data-phone-inbox]");
  const phoneReadingContext = required("[data-phone-reading-context]");
  const breadcrumb = required("[data-reading-breadcrumb]");
  const readingStatus = required("[data-reading-status]");
  const previousButton = required("[data-brief-previous]");
  const nextButton = required("[data-brief-next]");
  const expandLink = required("[data-brief-expand]");
  const navTitle = required("[data-nav-decision-title]");
  const message = required("[data-nav-message]");
  const partOutline = required("[data-part-outline]");
  const meterFill = required("[data-nav-meter-fill]");
  const meterLabel = required("[data-nav-meter-label]");
  const sendButton = required("[data-send-ruling]");
  const draftNote = required("[data-draft-note]");
  const ribbon = required("[data-ruling-ribbon]");
  const ribbonMessage = required("[data-ribbon-message]");
  const ribbonBody = required("[data-ribbon-body]");
  const ribbonStepper = required("[data-ribbon-part-stepper]");
  const ribbonProgress = required("[data-ribbon-progress]");
  const ribbonSendButton = required("[data-ribbon-send]");

  const captureByCard = new WeakMap();
  const activePartByCard = new WeakMap();
  const state = {
    card: null,
    capture: null,
    activePartId: null,
    loadSequence: 0,
    frameLoadedSequence: 0,
    frameDocumentVouched: false,
    expectingChromeLoad: false,
    awaitingFirstCapture: false,
    pendingDraft: null,
    pendingCollect: null,
    submitting: false,
    filed: false,
    storageWarning: "",
    notice: "",
    noticeKind: "",
    pendingScrollPart: null,
    pendingScrollStartedAt: 0,
    pendingScrollTimer: null,
  };
  let collectTokenSequence = 0;

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

  function draftKey(issue) {
    return `${DRAFT_PREFIX}${issue}`;
  }

  function showMessage(text, kind = "") {
    message.textContent = text;
    message.className = "ruling-nav-message";
    if (kind) message.classList.add(`is-${kind}`);
    ribbonMessage.textContent = text;
    ribbonMessage.className = "ribbon-message";
    if (kind) ribbonMessage.classList.add(`is-${kind}`);
  }

  function clearPendingScroll() {
    if (state.pendingScrollTimer !== null) {
      window.clearTimeout(state.pendingScrollTimer);
    }
    state.pendingScrollPart = null;
    state.pendingScrollStartedAt = 0;
    state.pendingScrollTimer = null;
  }

  function beginPendingScroll(partId) {
    clearPendingScroll();
    const startedAt = Date.now();
    state.pendingScrollPart = partId;
    state.pendingScrollStartedAt = startedAt;
    state.pendingScrollTimer = window.setTimeout(() => {
      if (
        state.pendingScrollPart === partId &&
        state.pendingScrollStartedAt === startedAt
      ) {
        state.pendingScrollPart = null;
        state.pendingScrollStartedAt = 0;
        state.pendingScrollTimer = null;
      }
    }, PENDING_SCROLL_TIMEOUT_MS);
  }

  function resetCompanion(text) {
    clearPendingScroll();
    state.capture = null;
    state.activePartId = null;
    state.storageWarning = "";
    state.notice = "";
    state.noticeKind = "";
    partOutline.replaceChildren();
    ribbonStepper.replaceChildren();
    ribbonBody.hidden = true;
    ribbon.removeAttribute("aria-busy");
    meterFill.style.width = "0%";
    meterLabel.textContent = "0 of 0 decided";
    ribbonProgress.textContent = "0 of 0 decided";
    sendButton.hidden = false;
    sendButton.disabled = true;
    sendButton.classList.remove("is-filed");
    sendButton.textContent = "SEND RULING";
    sendButton.title = "Select a brief to begin a ruling";
    ribbonSendButton.hidden = false;
    ribbonSendButton.disabled = true;
    ribbonSendButton.classList.remove("is-filed");
    ribbonSendButton.textContent = "SEND RULING";
    ribbonSendButton.title = sendButton.title;
    draftNote.textContent =
      "draft persists on this device · decide every part to send";
    navTitle.textContent = state.card?.dataset.briefTitle || "Select a brief";
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
    navTitle.textContent = title;
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

  function knownPartIds() {
    return state.capture ? state.capture.parts.map((part) => part.id) : [];
  }

  function scrollBriefToPart(partId) {
    const target = frame.contentWindow;
    if (!target) return;
    beginPendingScroll(partId);
    target.postMessage(
      {source: CHROME_MESSAGE_SOURCE, type: "scroll-to", axis: partId},
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

  function restoreBriefDraft(form) {
    const target = frame.contentWindow;
    if (!target) return;
    target.postMessage(
      {source: CHROME_MESSAGE_SOURCE, type: "restore", form},
      "*",
    );
  }

  function newCollectToken() {
    collectTokenSequence += 1;
    const random =
      typeof window.crypto?.randomUUID === "function"
        ? window.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${random}:${collectTokenSequence}`;
  }

  function collectFreshRuling(card) {
    const target = frame.contentWindow;
    if (!target || !state.frameDocumentVouched) {
      return Promise.reject(
        new DefinitelyNotFiledError(
          "The selected brief is not a chrome-vouched document; re-select it before sending.",
        ),
      );
    }
    if (state.pendingCollect) {
      return Promise.reject(
        new DefinitelyNotFiledError(
          "A fresh ruling capture is already pending.",
        ),
      );
    }

    const token = newCollectToken();
    return new Promise((resolve, reject) => {
      const timeoutId = window.setTimeout(() => {
        if (state.pendingCollect?.token !== token) return;
        state.pendingCollect = null;
        reject(
          new AmbiguousSubmissionError(
            "Fresh ruling capture timed out; filing status is uncertain.",
          ),
        );
      }, COLLECT_TIMEOUT_MS);
      state.pendingCollect = {card, reject, resolve, timeoutId, token};
      try {
        target.postMessage(makeCollectMessage(token), "*");
      } catch (error) {
        window.clearTimeout(timeoutId);
        state.pendingCollect = null;
        const detail = error instanceof Error ? error.message : String(error);
        reject(
          new AmbiguousSubmissionError(
            `Fresh ruling capture could not be requested: ${detail}`,
          ),
        );
      }
    });
  }

  function acceptFreshRuling(ruling) {
    const pending = state.pendingCollect;
    if (
      !pending ||
      pending.card !== state.card ||
      !rulingMatchesPendingToken(ruling, pending.token)
    ) {
      return false;
    }
    window.clearTimeout(pending.timeoutId);
    state.pendingCollect = null;
    pending.resolve(ruling);
    return true;
  }

  function setActivePart(partId, {scrollBrief = false} = {}) {
    if (!knownPartIds().includes(partId)) return;
    state.activePartId = partId;
    if (state.card) activePartByCard.set(state.card, partId);
    renderPartNavigation();
    if (scrollBrief) scrollBriefToPart(partId);
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

  function readDraft(issue) {
    let raw;
    try {
      raw = localStorage.getItem(draftKey(issue));
    } catch (error) {
      state.storageWarning = `Draft storage is unavailable: ${error.message}`;
      return null;
    }
    if (raw === null) {
      state.storageWarning = "";
      return null;
    }
    try {
      const draft = JSON.parse(raw);
      if (!isValidDraftRecord(draft)) {
        throw new TypeError("saved draft is not a shape-aware v3 record");
      }
      state.storageWarning = "";
      return draft;
    } catch (error) {
      state.storageWarning = `Saved draft for issue ${issue} is invalid: ${error.message}`;
      return null;
    }
  }

  function saveDraft(issue, form) {
    try {
      localStorage.setItem(
        draftKey(issue),
        JSON.stringify(makeDraftRecord(form)),
      );
      state.storageWarning = "";
    } catch (error) {
      state.storageWarning = `Draft could not be saved: ${error.message}`;
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

  function discardMismatchedDraft(issue) {
    try {
      localStorage.removeItem(draftKey(issue));
      return "";
    } catch (error) {
      return `The saved draft no longer matches this brief, but could not be cleared: ${error.message}`;
    }
  }

  function selectBrief(card) {
    if (state.card === card && state.frameDocumentVouched) return;
    showPhoneBrief();
    state.loadSequence += 1;
    state.frameLoadedSequence = 0;
    state.frameDocumentVouched = false;
    state.expectingChromeLoad = true;
    state.awaitingFirstCapture = false;
    state.card = card;
    state.submitting = card.dataset.rulingSubmissionPending === "true";
    state.filed =
      card.dataset.briefStatus === "archived" ||
      Boolean(card.dataset.rulingSubmissionUncertain);
    for (const candidate of allCards()) {
      candidate.setAttribute("aria-current", String(candidate === card));
    }

    setToolbar(card);
    resetCompanion("Loading brief capture…");
    navTitle.textContent = card.dataset.briefTitle || "Untitled decision";
    const cachedCapture = captureByCard.get(card) || null;
    if (cachedCapture) {
      state.capture = cachedCapture;
      const remembered = activePartByCard.get(card);
      state.activePartId = cachedCapture.parts.some(
        (part) => part.id === remembered,
      )
        ? remembered
        : cachedCapture.parts[0]?.id || null;
    }
    state.pendingDraft = readDraft(card.dataset.briefIssue || "");
    renderCompanion();

    frame.src = briefPath(card.dataset.briefName || "");
    frame.hidden = false;
    readingEmpty.hidden = true;
  }

  function updateCardProgress(count, total) {
    if (!state.card) return;
    state.card.dataset.partCount = String(total);
    const fill = state.card.querySelector(".brief-progress-track > span");
    const label = state.card.querySelector(".brief-progress-label");
    if (fill) fill.style.width = `${total ? (count / total) * 100 : 0}%`;
    if (label) label.textContent = `${count}/${total} parts`;
  }

  function renderOutline() {
    partOutline.replaceChildren();
    if (!state.capture) return;
    for (const part of state.capture.parts) {
      const item = make("li", "ruling-nav-item");
      const button = make("button", "ruling-nav-link");
      button.type = "button";
      button.dataset.partId = part.id;
      button.classList.toggle("is-active", state.activePartId === part.id);
      button.classList.toggle("is-answered", part.answered);
      button.setAttribute(
        "aria-current",
        state.activePartId === part.id ? "true" : "false",
      );
      const glyph = make(
        "span",
        "ruling-nav-glyph",
        part.answered ? "✓" : "○",
      );
      glyph.setAttribute("aria-hidden", "true");
      button.append(glyph, make("span", "ruling-nav-label", part.label || part.id));
      button.addEventListener("click", () => {
        setActivePart(part.id, {scrollBrief: true});
      });
      item.append(button);
      partOutline.append(item);
    }
  }

  function renderRibbonParts() {
    ribbonStepper.replaceChildren();
    if (!state.capture) return;
    for (const part of state.capture.parts) {
      const active = state.activePartId === part.id;
      const dot = make("button", "ribbon-part-dot");
      dot.type = "button";
      dot.classList.add(
        active ? "is-active" : part.answered ? "is-answered" : "is-unanswered",
      );
      dot.setAttribute("aria-pressed", String(active));
      dot.setAttribute(
        "aria-label",
        `${part.label || part.id}: ${active ? "in view" : part.answered ? "decided" : "undecided"}`,
      );
      dot.title = `${part.label || part.id} — ${active ? "in view" : part.answered ? "decided" : "undecided"}`;
      dot.addEventListener("click", () => {
        setActivePart(part.id, {scrollBrief: true});
      });
      ribbonStepper.append(dot);
    }
  }

  function renderPartNavigation() {
    renderOutline();
    renderRibbonParts();
  }

  function renderCompanion() {
    const card = state.card;
    if (!card) return;
    const capture = state.capture;
    const total = capture?.parts.length || 0;
    const count = capture
      ? capture.parts.filter((part) => part.answered).length
      : 0;
    const archived = card.dataset.briefStatus === "archived";
    const pending =
      state.submitting || card.dataset.rulingSubmissionPending === "true";
    const uncertain = card.dataset.rulingSubmissionUncertain || "";
    const rejected = card.dataset.rulingSubmissionError || "";
    const complete = Boolean(capture?.allAnswered) && state.frameDocumentVouched;

    navTitle.textContent = card.dataset.briefTitle || "Untitled decision";
    meterFill.style.width = `${total ? (count / total) * 100 : 0}%`;
    meterLabel.textContent = `${count} of ${total} decided`;
    ribbonProgress.textContent = `${count} of ${total} decided`;
    ribbonBody.hidden = !capture;
    renderPartNavigation();
    updateCardProgress(count, total);

    sendButton.hidden = archived;
    ribbonSendButton.hidden = archived;
    const canSend = complete && !pending && !uncertain && !archived;
    sendButton.disabled = !canSend;
    ribbonSendButton.disabled = !canSend;
    sendButton.classList.toggle("is-filed", archived);
    ribbonSendButton.classList.toggle("is-filed", archived);
    ribbon.removeAttribute("aria-busy");

    if (archived) {
      sendButton.textContent = "RULING FILED";
      ribbonSendButton.textContent = "FILED";
      draftNote.textContent = "one ruling per brief · no archived re-filing";
      showMessage(
        state.notice ||
          `Ruling ${card.dataset.rulingSequence || "filed"} already acknowledges this brief.`,
        state.noticeKind || "acknowledged",
      );
      return;
    }
    if (uncertain) {
      sendButton.textContent = "STATUS UNCERTAIN";
      sendButton.title = "Reload and check the Archive before resubmitting";
      ribbonSendButton.textContent = "UNCERTAIN";
      ribbonSendButton.title = sendButton.title;
      draftNote.textContent = "reload · check archive before any resubmission";
      showMessage(
        `Ruling submission status is UNCERTAIN: ${uncertain}. Reload and check the Archive before resubmitting.`,
        "error",
      );
      return;
    }
    if (pending) {
      sendButton.textContent = "FILING…";
      sendButton.title = "Ruling submission is in progress";
      ribbonSendButton.textContent = "FILING…";
      ribbonSendButton.title = sendButton.title;
      ribbon.setAttribute("aria-busy", "true");
      showMessage("Filing one ruling for this brief…");
      return;
    }

    sendButton.textContent = "SEND RULING";
    ribbonSendButton.textContent = "SEND RULING";
    const incomplete = total - count;
    sendButton.title = !state.frameDocumentVouched
      ? "Waiting for the chrome-loaded brief document"
      : !capture
      ? "Waiting for the brief to report its capture state"
      : incomplete
        ? `${incomplete} decision ${incomplete === 1 ? "part is" : "parts are"} incomplete`
        : "File one ruling for this brief";
    ribbonSendButton.title = sendButton.title;
    draftNote.textContent = state.storageWarning
      ? "draft storage failed · this ruling is not persisted"
      : "draft persists on this device · decide every part to send";
    if (rejected) {
      showMessage(`Ruling was not filed: ${rejected}`, "error");
    } else if (state.storageWarning) {
      showMessage(state.storageWarning, "error");
    } else if (state.notice) {
      showMessage(state.notice, state.noticeKind);
    } else if (!state.frameDocumentVouched || !capture) {
      showMessage("Loading brief capture…");
    } else {
      showMessage("");
    }
  }

  function acceptCapture(capture) {
    const card = state.card;
    if (!card) return;
    const expectedIssue = card.dataset.briefIssue || "";
    if (!capture.issue || capture.issue !== expectedIssue) {
      state.capture = null;
      partOutline.replaceChildren();
      ribbonStepper.replaceChildren();
      sendButton.disabled = true;
      ribbonSendButton.disabled = true;
      showMessage(
        `Brief capture issue ${capture.issue || "(empty)"} does not match inbox issue ${expectedIssue || "(empty)"}.`,
        "error",
      );
      return;
    }

    const firstCapture = state.capture === null;
    const firstDocumentCapture = state.awaitingFirstCapture;
    let draftToRestore = null;
    let shouldSaveDraft = true;
    if (firstDocumentCapture) {
      state.awaitingFirstCapture = false;
      const savedDraft = state.pendingDraft;
      state.pendingDraft = null;
      if (savedDraft && draftMatchesForm(savedDraft, capture.form)) {
        draftToRestore = savedDraft.form;
        shouldSaveDraft = false;
      } else if (savedDraft) {
        const warning = discardMismatchedDraft(capture.issue);
        state.storageWarning = warning;
        state.notice = warning ||
          "The saved draft was discarded because this brief's form shape changed.";
        state.noticeKind = warning ? "error" : "";
      }
    }
    state.capture = capture;
    captureByCard.set(card, capture);
    const remembered = activePartByCard.get(card);
    if (!capture.parts.some((part) => part.id === state.activePartId)) {
      state.activePartId = capture.parts.some((part) => part.id === remembered)
        ? remembered
        : capture.parts[0]?.id || null;
    }
    if (state.activePartId) activePartByCard.set(card, state.activePartId);
    if (
      shouldSaveDraft &&
      card.dataset.briefStatus === "awaiting" &&
      !card.dataset.rulingSubmissionUncertain
    ) {
      saveDraft(capture.issue, capture.form);
    }
    renderCompanion();
    if (draftToRestore) restoreBriefDraft(draftToRestore);
    if (firstCapture) requestBriefInView();
  }

  function invalidateForeignFrameDocument() {
    const card = state.card;
    state.expectingChromeLoad = false;
    state.frameDocumentVouched = false;
    state.frameLoadedSequence = 0;
    state.awaitingFirstCapture = false;
    state.pendingDraft = null;
    state.capture = null;
    state.activePartId = null;
    clearPendingScroll();
    if (card) {
      captureByCard.delete(card);
      activePartByCard.delete(card);
      updateCardProgress(0, 0);
    }
    state.notice =
      "The brief navigated away from the chrome-loaded document. Re-select it to reload capture safely.";
    state.noticeKind = "error";
    renderCompanion();
  }

  function handleFrameLoad() {
    if (state.expectingChromeLoad) {
      state.expectingChromeLoad = false;
      state.frameLoadedSequence = state.loadSequence;
      state.frameDocumentVouched = true;
      state.awaitingFirstCapture = true;
      requestBriefInView();
      return;
    }
    if (state.card) invalidateForeignFrameDocument();
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
    const capture = state.capture;
    const submittedCard = state.card;
    if (
      !capture ||
      !capture.allAnswered ||
      !submittedCard ||
      !state.frameDocumentVouched
    ) {
      showMessage("Ruling is not ready: every decision part must be answered.", "error");
      return;
    }
    if (capture.issue !== submittedCard.dataset.briefIssue) {
      showMessage(
        "Ruling is not ready: the brief reported a mismatched issue.",
        "error",
      );
      return;
    }

    const submittedIssue = submittedCard.dataset.briefIssue || "";
    delete submittedCard.dataset.rulingSubmissionError;
    delete submittedCard.dataset.rulingSubmissionUncertain;
    submittedCard.dataset.rulingSubmissionPending = "true";
    state.submitting = true;
    renderCompanion();

    let acknowledgement;
    let submittedCapture;
    try {
      const ruling = await collectFreshRuling(submittedCard);
      if (!ruling.allAnswered) {
        throw new DefinitelyNotFiledError(
          "The fresh ruling is incomplete; every decision part must be answered.",
        );
      }
      if (!ruling.markdown.trim()) {
        throw new DefinitelyNotFiledError(
          "The fresh ruling has empty markdown and was not filed.",
        );
      }
      const payload = {
        issue: submittedIssue,
        markdown: ruling.markdown,
        form: ruling.form,
      };
      submittedCapture = {
        ...capture,
        allAnswered: ruling.allAnswered,
        form: ruling.form,
        markdown: ruling.markdown,
      };
      const response = await fetch("/ruling", {
        method: "POST",
        headers: {"Content-Type": "application/json", Accept: "application/json"},
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      acknowledgement = await readRulingAcknowledgement(response, submittedIssue);
    } catch (error) {
      delete submittedCard.dataset.rulingSubmissionPending;
      const stillSelected = state.card === submittedCard;
      const detail = error instanceof Error ? error.message : String(error);
      if (submissionFailureKind(error) === "definitely-not-filed") {
        submittedCard.dataset.rulingSubmissionError = detail;
        if (stillSelected) {
          state.submitting = false;
          state.filed = false;
          renderCompanion();
        }
      } else {
        submittedCard.dataset.rulingSubmissionUncertain = detail;
        delete submittedCard.dataset.rulingSubmissionError;
        if (stillSelected) {
          state.submitting = false;
          state.filed = true;
          renderCompanion();
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
    captureByCard.set(submittedCard, submittedCapture);
    archiveCurrentCard(submittedCard, acknowledgement, stillSelected);
    if (stillSelected) {
      state.capture = submittedCapture;
      state.filed = true;
      state.submitting = false;
      state.notice =
        `Ruling ${acknowledgement.sequence || "filed"} was filed and this brief is now archived.${clearWarning}`;
      state.noticeKind = "acknowledged";
      renderCompanion();
    } else if (clearWarning) {
      console.error(clearWarning.trim());
    }
  }

  function resizeFromKeyboard(resizer, direction) {
    const isList = resizer.dataset.resizer === "list";
    const target = isList ? listPane : navPane;
    const minimum = isList ? LIST_MIN : NAV_MIN;
    const maximum = isList ? LIST_MAX : NAV_MAX;
    const current = target.getBoundingClientRect().width;
    let next = current;
    if (direction === "minimum") next = minimum;
    if (direction === "maximum") next = maximum;
    if (direction === "decrease") next = current - 10;
    if (direction === "increase") next = current + 10;
    next = clamp(next, minimum, maximum);
    shell.style.setProperty(
      isList ? "--list-width" : "--nav-width",
      `${next}px`,
    );
    resizer.setAttribute("aria-valuenow", String(Math.round(next)));
  }

  function wireResizer(resizer) {
    const isList = resizer.dataset.resizer === "list";
    const target = isList ? listPane : navPane;
    const minimum = isList ? LIST_MIN : NAV_MIN;
    const maximum = isList ? LIST_MAX : NAV_MAX;

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
          isList ? "--list-width" : "--nav-width",
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
      selectBrief(card);
    });
  }
  for (const resizer of shell.querySelectorAll("[data-resizer]")) {
    wireResizer(resizer);
  }

  previousButton.addEventListener("click", () => {
    const cards = pendingCards();
    const index = cards.indexOf(state.card);
    if (index > 0) selectBrief(cards[index - 1]);
  });
  nextButton.addEventListener("click", () => {
    const cards = pendingCards();
    const index = cards.indexOf(state.card);
    if (index >= 0 && index < cards.length - 1) {
      selectBrief(cards[index + 1]);
    }
  });
  expandLink.addEventListener("click", (event) => {
    if (expandLink.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
    }
  });
  phoneInboxButton.addEventListener("click", showPhoneInbox);
  sendButton.addEventListener("click", () => void fileRuling());
  ribbonSendButton.addEventListener("click", () => void fileRuling());
  frame.addEventListener("load", handleFrameLoad);
  window.addEventListener("message", (event) => {
    // contentWindow is a persistent WindowProxy across iframe navigations.
    if (event.source !== frame.contentWindow) return;
    if (
      !isMessageFromCurrentBrief(
        event.source,
        frame.contentWindow,
        state.frameDocumentVouched,
        state.frameLoadedSequence,
        state.loadSequence,
      )
    ) {
      return;
    }
    if (isValidBriefRulingMessage(event.data)) {
      acceptFreshRuling(event.data);
      return;
    }
    if (isValidBriefCaptureMessage(event.data)) {
      acceptCapture(event.data);
      return;
    }
    if (!isValidBriefInViewMessage(event.data, knownPartIds())) return;
    if (
      !shouldAcceptInViewReport(state.pendingScrollPart, event.data.axis)
    ) {
      return;
    }
    if (state.pendingScrollPart === event.data.axis) clearPendingScroll();
    setActivePart(event.data.axis);
  });

  updateNavigation();
  const initial = pendingCards()[0];
  const startsOnPhone =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 760px)").matches;
  if (initial && !startsOnPhone) {
    selectBrief(initial);
  }
})();
