(() => {
  "use strict";

  const DRAFT_PREFIX = "arachne:draft:v3:";
  const BRIEF_MESSAGE_SOURCE = "arachne-brief";
  const CHROME_MESSAGE_SOURCE = "arachne-chrome";
  const COLLECT_TIMEOUT_MS = 1500;
  const PENDING_SCROLL_TIMEOUT_MS = 700;
  const DISMISS_CONFIRM_MS = 8000;
  const LIST_MIN = 240;
  const LIST_MAX = 440;
  const NAV_MIN = 260;
  const NAV_MAX = 420;
  const FOREGROUND_RELOAD_AGE_MS = 60000;

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

  function computeKeyboardInset(
    layoutViewportHeight,
    visualViewportHeight,
    visualViewportOffsetTop = 0,
  ) {
    const layoutHeight = Number(layoutViewportHeight);
    const visualHeight = Number(visualViewportHeight);
    const offsetTop = Number(visualViewportOffsetTop);
    if (
      !Number.isFinite(layoutHeight) ||
      !Number.isFinite(visualHeight) ||
      !Number.isFinite(offsetTop)
    ) {
      return 0;
    }
    return Math.max(0, layoutHeight - visualHeight - offsetTop);
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

  function isValidShareResponse(data) {
    if (
      !hasExactKeys(data, [
        "content_sha256",
        "created_at",
        "expires_at",
        "id",
        "markdown_url",
        "reused",
        "url",
      ]) ||
      typeof data.id !== "string" ||
      !/^[A-Za-z0-9_-]{32}$/.test(data.id) ||
      typeof data.content_sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(data.content_sha256) ||
      typeof data.created_at !== "string" ||
      typeof data.expires_at !== "string" ||
      typeof data.url !== "string" ||
      typeof data.markdown_url !== "string" ||
      typeof data.reused !== "boolean"
    ) {
      return false;
    }
    const created = Date.parse(data.created_at);
    const expires = Date.parse(data.expires_at);
    if (
      !Number.isFinite(created) ||
      !Number.isFinite(expires) ||
      expires - created !== 30 * 24 * 60 * 60 * 1000
    ) {
      return false;
    }
    let publicUrl;
    let markdownUrl;
    try {
      publicUrl = new URL(data.url);
      markdownUrl = new URL(data.markdown_url);
    } catch (_error) {
      return false;
    }
    if (
      !["http:", "https:"].includes(publicUrl.protocol) ||
      publicUrl.username ||
      publicUrl.password ||
      publicUrl.search ||
      publicUrl.hash ||
      markdownUrl.origin !== publicUrl.origin ||
      markdownUrl.username ||
      markdownUrl.password ||
      markdownUrl.search ||
      markdownUrl.hash ||
      publicUrl.pathname !== `/s/${data.id}` ||
      markdownUrl.pathname !== `/s/${data.id}.md`
    ) {
      return false;
    }
    return true;
  }

  function isValidDismissalResponse(data) {
    return (
      hasExactKeys(data, [
        "dismissed_at",
        "issue",
        "kind",
        "ok",
        "page",
        "published_at_ms",
        "reused",
      ]) &&
      data.ok === true &&
      data.kind === "dismissal" &&
      typeof data.page === "string" &&
      data.page.length > 0 &&
      typeof data.issue === "string" &&
      data.issue.length > 0 &&
      Number.isSafeInteger(data.published_at_ms) &&
      data.published_at_ms >= 0 &&
      typeof data.dismissed_at === "string" &&
      Number.isFinite(Date.parse(data.dismissed_at)) &&
      typeof data.reused === "boolean"
    );
  }

  function hasOtherAwaitingBriefForIssue(cards, currentCard, issue) {
    return cards.some(
      (card) =>
        card !== currentCard &&
        card?.dataset?.briefStatus === "awaiting" &&
        card.dataset.briefIssue === issue,
    );
  }

  async function readShareResponse(response) {
    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new Error(
        `HTTP ${response.status} share response was not valid JSON: ${error.message}`,
      );
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload.detail === "string"
          ? payload.detail.trim()
          : "";
      throw new Error(detail || `the server returned HTTP ${response.status}`);
    }
    if (!isValidShareResponse(payload)) {
      throw new Error("the server returned an invalid public-share record");
    }
    return payload;
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

  async function readDismissalAcknowledgement(
    response,
    submittedPage,
    submittedIssue,
    submittedPublishedAtMs,
  ) {
    let acknowledgement;
    try {
      acknowledgement = await response.json();
    } catch (error) {
      const bodyKind = response.ok ? "acknowledgement" : "error response";
      throw new AmbiguousSubmissionError(
        `HTTP ${response.status} dismissal ${bodyKind} was not valid JSON: ${error.message}`,
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
        `the server returned HTTP ${response.status} without a readable dismissal error detail`,
      );
    }
    if (!isValidDismissalResponse(acknowledgement)) {
      throw new AmbiguousSubmissionError(
        "the server returned an invalid dismissal acknowledgement",
      );
    }
    if (
      acknowledgement.page !== submittedPage ||
      acknowledgement.issue !== submittedIssue ||
      acknowledgement.published_at_ms !== submittedPublishedAtMs
    ) {
      throw new AmbiguousSubmissionError(
        "the dismissal acknowledgement does not match the submitted brief publication",
      );
    }
    return acknowledgement;
  }

  function submissionFailureKind(error) {
    return error instanceof DefinitelyNotFiledError
      ? "definitely-not-filed"
      : "ambiguous";
  }

  function formatUtcMoment(date) {
    return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze({
      computeKeyboardInset,
      draftMatchesForm,
      formatUtcMoment,
      formShapeFingerprint,
      isValidBriefCaptureMessage,
      isValidBriefInViewMessage,
      isValidBriefRulingMessage,
      isValidDismissalResponse,
      isValidShareResponse,
      hasOtherAwaitingBriefForIssue,
      isMessageFromCurrentBrief,
      makeCollectMessage,
      makeDraftRecord,
      readRulingAcknowledgement,
      readDismissalAcknowledgement,
      readShareResponse,
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

  if (window.visualViewport) {
    const visualViewport = window.visualViewport;
    let keyboardInsetFramePending = false;

    function updateKeyboardInset() {
      keyboardInsetFramePending = false;
      const inset = computeKeyboardInset(
        window.innerHeight,
        visualViewport.height,
        visualViewport.offsetTop,
      );
      shell.style.setProperty("--keyboard-inset", `${inset}px`);
    }

    function scheduleKeyboardInsetUpdate() {
      if (keyboardInsetFramePending) return;
      keyboardInsetFramePending = true;
      window.requestAnimationFrame(updateKeyboardInset);
    }

    visualViewport.addEventListener("resize", scheduleKeyboardInsetUpdate);
    visualViewport.addEventListener("scroll", scheduleKeyboardInsetUpdate, {
      passive: true,
    });
    scheduleKeyboardInsetUpdate();
  }

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
  const shareButton = required("[data-share-brief]");
  const expandLink = required("[data-brief-expand]");
  const reloadButton = required("[data-reload-inbox]");
  const archiveDisclosure = required("[data-archive-disclosure]");
  const archivePanel = required('[data-list-panel="archive"]');
  const readingMeta = required("[data-reading-meta]");
  const readingMetaIssue = required("[data-reading-meta-issue]");
  const readingMetaTitle = required("[data-reading-meta-title]");
  const readingMetaDetail = required("[data-reading-meta-detail]");
  const shareResult = required("[data-share-result]");
  const shareStatus = required("[data-share-status]");
  const shareExpiry = required("[data-share-expiry]");
  const shareLink = required("[data-share-link]");
  const shareCopyButton = required("[data-share-copy]");
  const shareMarkdownLink = required("[data-share-markdown]");
  const shareRevokeButton = required("[data-share-revoke]");
  const shareCloseButton = required("[data-share-close]");
  const navTitle = required("[data-nav-decision-title]");
  const message = required("[data-nav-message]");
  const partOutline = required("[data-part-outline]");
  const meterFill = required("[data-nav-meter-fill]");
  const meterLabel = required("[data-nav-meter-label]");
  const sendButton = required("[data-send-ruling]");
  const dismissButton = required("[data-dismiss-brief]");
  const draftNote = required("[data-draft-note]");
  const ribbon = required("[data-ruling-ribbon]");
  const ribbonMessage = required("[data-ribbon-message]");
  const ribbonBody = required("[data-ribbon-body]");
  const ribbonStepper = required("[data-ribbon-part-stepper]");
  const ribbonProgress = required("[data-ribbon-progress]");
  const ribbonDismissButton = required("[data-ribbon-dismiss]");
  const ribbonSendButton = required("[data-ribbon-send]");

  const captureByCard = new WeakMap();
  const activePartByCard = new WeakMap();
  const shareByCard = new WeakMap();
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
    dismissing: false,
    dismissArmed: false,
    dismissArmTimer: null,
    filed: false,
    storageWarning: "",
    notice: "",
    noticeKind: "",
    pendingScrollPart: null,
    pendingScrollStartedAt: 0,
    pendingScrollTimer: null,
    sharing: false,
    revokingShare: false,
  };
  let collectTokenSequence = 0;
  let inFlightFetches = 0;
  let foregroundAgeStartedAt = Date.now();
  let reloadRequested = false;
  let staleReloadPending = false;

  function cancelDismissConfirmation() {
    if (state.dismissArmTimer !== null) {
      window.clearTimeout(state.dismissArmTimer);
      state.dismissArmTimer = null;
    }
    state.dismissArmed = false;
  }

  function armDismissConfirmation() {
    cancelDismissConfirmation();
    state.dismissArmed = true;
    state.dismissArmTimer = window.setTimeout(() => {
      state.dismissArmTimer = null;
      state.dismissArmed = false;
      renderCompanion();
    }, DISMISS_CONFIRM_MS);
    renderCompanion();
  }

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

  function setShareButtonState(label, disabled) {
    const accessibleLabel =
      label === "SHARE"
        ? "Share"
        : label === "SHARING…"
          ? "Sharing…"
          : "Revoking…";
    const shareState =
      label === "SHARE"
        ? "share"
        : label === "SHARING…"
          ? "sharing"
          : "revoking";
    shareButton.disabled = disabled;
    shareButton.setAttribute("aria-label", accessibleLabel);
    shareButton.title = accessibleLabel;
    shareButton.dataset.shareState = shareState;
  }

  async function trackedFetch(operation) {
    inFlightFetches += 1;
    try {
      return await operation();
    } finally {
      inFlightFetches -= 1;
      if (inFlightFetches === 0 && staleReloadPending) maybeReloadStaleInbox();
    }
  }

  function phoneReadingActive() {
    // selectBrief() sets is-phone-reading on every layout; the compact reading
    // mode only exists under the phone media query.
    return (
      shell.classList.contains("is-phone-reading") &&
      window.matchMedia("(max-width: 760px)").matches
    );
  }

  function maybeReloadStaleInbox() {
    if (
      reloadRequested ||
      document.visibilityState !== "visible" ||
      Date.now() - foregroundAgeStartedAt < FOREGROUND_RELOAD_AGE_MS ||
      phoneReadingActive()
    ) {
      staleReloadPending = false;
      return;
    }
    if (inFlightFetches > 0) {
      staleReloadPending = true;
      return;
    }
    staleReloadPending = false;
    reloadRequested = true;
    window.location.reload();
  }

  function hideShareResult() {
    shareResult.hidden = true;
    shareResult.classList.remove("is-error");
  }

  function showShareError(detail, record = null) {
    if (record) {
      showShareRecord(record, false);
      shareResult.classList.add("is-error");
      shareStatus.textContent = "Public link could not be revoked";
      shareExpiry.textContent = detail;
      return;
    }
    shareResult.hidden = false;
    shareResult.classList.add("is-error");
    shareStatus.textContent = "Public link was not created";
    shareExpiry.textContent = detail;
    shareLink.value = "";
    shareLink.parentElement.hidden = true;
    shareMarkdownLink.parentElement.hidden = true;
  }

  function showShareRecord(record, copied) {
    shareResult.hidden = false;
    shareResult.classList.remove("is-error");
    shareStatus.textContent = copied ? "Public link copied" : "Public link ready";
    shareExpiry.textContent = `Expires ${new Date(record.expires_at).toLocaleString()} · anyone with the link can read it`;
    shareLink.value = record.url;
    shareLink.parentElement.hidden = false;
    shareMarkdownLink.href = record.markdown_url;
    shareMarkdownLink.parentElement.hidden = false;
    shareCopyButton.disabled = false;
    shareRevokeButton.disabled = false;
  }

  async function copyShareLink() {
    const text = shareLink.value;
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_error) {
      shareLink.focus();
      shareLink.select();
      shareLink.setSelectionRange(0, text.length);
      try {
        return document.execCommand("copy");
      } catch (_fallbackError) {
        return false;
      }
    }
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
    cancelDismissConfirmation();
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
    dismissButton.hidden = false;
    dismissButton.disabled = true;
    dismissButton.textContent = "DISMISS BRIEF";
    dismissButton.title = "Select a brief to dismiss";
    ribbonSendButton.hidden = false;
    ribbonSendButton.disabled = true;
    ribbonSendButton.classList.remove("is-filed");
    ribbonSendButton.textContent = "SEND RULING";
    ribbonSendButton.title = sendButton.title;
    ribbonDismissButton.hidden = false;
    ribbonDismissButton.disabled = true;
    ribbonDismissButton.textContent = "DISMISS";
    ribbonDismissButton.title = dismissButton.title;
    draftNote.textContent =
      "draft persists on this device · decide every part to send";
    navTitle.textContent = state.card?.dataset.briefTitle || "Select a brief";
    showMessage(text);
  }

  function setToolbar(card) {
    const issue = card.dataset.briefIssue || "";
    const title = card.dataset.briefTitle || "Untitled decision";
    const archived = card.dataset.briefStatus === "archived";
    const dismissed = archived && card.dataset.archiveKind === "dismissal";
    breadcrumb.textContent = `DECISION #${issue} — ${title}`;
    phoneReadingContext.textContent = `#${issue} · ${title}`;
    readingStatus.textContent = dismissed
      ? "DISMISSED"
      : archived
        ? "ARCHIVED"
        : "AWAITING";
    readingStatus.hidden = false;
    readingMetaIssue.textContent = `#${issue}`;
    readingMetaTitle.textContent = title;
    readingMetaDetail.textContent = dismissed
      ? `${card.dataset.briefTimestamp || ""} · no ruling`
      : archived && card.dataset.rulingSequence
        ? `${card.dataset.briefTimestamp || ""} · ruling ${card.dataset.rulingSequence}`
        : card.dataset.briefTimestamp || "";
    readingMeta.hidden = false;
    expandLink.href = briefPath(card.dataset.briefName || "");
    expandLink.setAttribute("aria-disabled", "false");
    setShareButtonState("SHARE", state.sharing || state.revokingShare);
    const shared = shareByCard.get(card);
    if (shared) showShareRecord(shared, false);
    else hideShareResult();
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

  function clearDraft(issue, completion = "The operation completed") {
    try {
      localStorage.removeItem(draftKey(issue));
      return "";
    } catch (error) {
      return ` ${completion}, but its local draft could not be cleared: ${error.message}`;
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
    state.dismissing = card.dataset.dismissalSubmissionPending === "true";
    state.dismissArmed = false;
    state.filed =
      card.dataset.briefStatus === "archived" ||
      Boolean(card.dataset.rulingSubmissionUncertain) ||
      Boolean(card.dataset.dismissalSubmissionUncertain);
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
    const dismissed = archived && card.dataset.archiveKind === "dismissal";
    const rulingPending =
      state.submitting || card.dataset.rulingSubmissionPending === "true";
    const dismissalPending =
      state.dismissing || card.dataset.dismissalSubmissionPending === "true";
    const pending = rulingPending || dismissalPending;
    const rulingUncertain = card.dataset.rulingSubmissionUncertain || "";
    const dismissalUncertain = card.dataset.dismissalSubmissionUncertain || "";
    const uncertain = rulingUncertain || dismissalUncertain;
    const rulingRejected = card.dataset.rulingSubmissionError || "";
    const dismissalRejected = card.dataset.dismissalSubmissionError || "";
    const complete = Boolean(capture?.allAnswered) && state.frameDocumentVouched;
    const publishedAtMs = Number(card.dataset.briefPublishedAtMs);

    navTitle.textContent = card.dataset.briefTitle || "Untitled decision";
    meterFill.style.width = `${total ? (count / total) * 100 : 0}%`;
    meterLabel.textContent = `${count} of ${total} decided`;
    ribbonProgress.textContent = `${count} of ${total} decided`;
    ribbonBody.hidden = false;
    renderPartNavigation();
    updateCardProgress(count, total);

    sendButton.hidden = archived;
    ribbonSendButton.hidden = archived;
    dismissButton.hidden = archived;
    ribbonDismissButton.hidden = archived;
    const canSend =
      complete && !pending && !uncertain && !archived && !state.dismissArmed;
    const canDismiss =
      !pending &&
      !uncertain &&
      !archived &&
      Number.isSafeInteger(publishedAtMs) &&
      publishedAtMs >= 0;
    sendButton.disabled = !canSend;
    ribbonSendButton.disabled = !canSend;
    dismissButton.disabled = !canDismiss;
    ribbonDismissButton.disabled = !canDismiss;
    sendButton.classList.toggle("is-filed", archived);
    ribbonSendButton.classList.toggle("is-filed", archived);
    dismissButton.classList.toggle("is-confirming", state.dismissArmed);
    ribbonDismissButton.classList.toggle("is-confirming", state.dismissArmed);
    ribbon.removeAttribute("aria-busy");

    if (archived) {
      state.dismissArmed = false;
      if (dismissed) {
        draftNote.textContent = "dismissed without ruling · republish to reopen";
        showMessage(
          state.notice ||
            "This brief was dismissed without filing a ruling or waking an agent.",
          state.noticeKind || "acknowledged",
        );
      } else {
        draftNote.textContent = "one ruling per brief · no archived re-filing";
        showMessage(
          state.notice ||
            `Ruling ${card.dataset.rulingSequence || "filed"} already acknowledges this brief.`,
          state.noticeKind || "acknowledged",
        );
      }
      return;
    }
    if (uncertain) {
      const noun = dismissalUncertain ? "Dismissal" : "Ruling";
      sendButton.textContent = "STATUS UNCERTAIN";
      sendButton.title = "Reload and check the Archive before trying again";
      ribbonSendButton.textContent = "UNCERTAIN";
      ribbonSendButton.title = sendButton.title;
      dismissButton.textContent = "STATUS UNCERTAIN";
      dismissButton.title = sendButton.title;
      ribbonDismissButton.textContent = "UNCERTAIN";
      ribbonDismissButton.title = sendButton.title;
      draftNote.textContent = "reload · check archive before any resubmission";
      showMessage(
        `${noun} status is UNCERTAIN: ${uncertain}. Reload and check the Archive before trying again.`,
        "error",
      );
      return;
    }
    if (pending) {
      const dismissing = dismissalPending;
      sendButton.textContent = dismissing ? "DISMISSING…" : "FILING…";
      sendButton.title = dismissing
        ? "Dismissal is in progress"
        : "Ruling submission is in progress";
      ribbonSendButton.textContent = dismissing ? "DISMISSING…" : "FILING…";
      ribbonSendButton.title = sendButton.title;
      dismissButton.textContent = dismissing ? "DISMISSING…" : "BUSY";
      dismissButton.title = sendButton.title;
      ribbonDismissButton.textContent = dismissing ? "DISMISSING…" : "BUSY";
      ribbonDismissButton.title = sendButton.title;
      ribbon.setAttribute("aria-busy", "true");
      showMessage(
        dismissing
          ? "Dismissing this brief without filing a ruling…"
          : "Filing one ruling for this brief…",
      );
      return;
    }

    sendButton.textContent = "SEND RULING";
    ribbonSendButton.textContent = "SEND RULING";
    dismissButton.textContent = state.dismissArmed
      ? "CONFIRM DISMISS"
      : "DISMISS BRIEF";
    ribbonDismissButton.textContent = state.dismissArmed
      ? "CONFIRM"
      : "DISMISS";
    dismissButton.title = state.dismissArmed
      ? "Confirm archive without filing a ruling"
      : "Archive this brief without filing a ruling";
    ribbonDismissButton.title = dismissButton.title;
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
    if (dismissalRejected) {
      showMessage(`Brief was not dismissed: ${dismissalRejected}`, "error");
    } else if (rulingRejected) {
      showMessage(`Ruling was not filed: ${rulingRejected}`, "error");
    } else if (state.dismissArmed) {
      showMessage(
        "Press CONFIRM DISMISS to archive this brief without a ruling. No agent will be woken.",
        "error",
      );
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
    const archiveKind =
      acknowledgement.kind === "dismissal" ? "dismissal" : "ruling";
    const sequence =
      archiveKind === "ruling" ? String(acknowledgement.sequence || "filed") : "";
    const archivedAt = new Date(
      archiveKind === "dismissal"
        ? acknowledgement.dismissed_at
        : acknowledgement.submitted_at || Date.now(),
    );
    card.dataset.briefStatus = "archived";
    card.dataset.archiveKind = archiveKind;
    card.dataset.rulingSequence = sequence;
    card.dataset.briefTimestamp = `${archiveKind === "dismissal" ? "dismissed" : "ruled"} ${formatUtcMoment(archivedAt)}`;

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
      setToolbar(card);
    }
    updateNavigation();
  }

  async function dismissCurrentBrief() {
    const dismissedCard = state.card;
    if (
      !dismissedCard ||
      dismissedCard.dataset.briefStatus === "archived" ||
      state.submitting ||
      state.dismissing ||
      dismissedCard.dataset.rulingSubmissionUncertain ||
      dismissedCard.dataset.dismissalSubmissionUncertain
    ) {
      return;
    }
    if (!state.dismissArmed) {
      armDismissConfirmation();
      return;
    }
    cancelDismissConfirmation();

    const page = dismissedCard.dataset.briefName || "";
    const issue = dismissedCard.dataset.briefIssue || "";
    const publishedAtMs = Number(dismissedCard.dataset.briefPublishedAtMs);
    if (!page || !issue || !Number.isSafeInteger(publishedAtMs) || publishedAtMs < 0) {
      dismissedCard.dataset.dismissalSubmissionError =
        "the inbox card has no valid publication identity";
      renderCompanion();
      return;
    }

    delete dismissedCard.dataset.dismissalSubmissionError;
    delete dismissedCard.dataset.dismissalSubmissionUncertain;
    dismissedCard.dataset.dismissalSubmissionPending = "true";
    state.dismissing = true;
    renderCompanion();

    let acknowledgement;
    try {
      const response = await trackedFetch(() =>
        fetch("/dismissals", {
          method: "POST",
          headers: {"Content-Type": "application/json", Accept: "application/json"},
          credentials: "same-origin",
          body: JSON.stringify({page, published_at_ms: publishedAtMs}),
        }),
      );
      acknowledgement = await readDismissalAcknowledgement(
        response,
        page,
        issue,
        publishedAtMs,
      );
    } catch (error) {
      delete dismissedCard.dataset.dismissalSubmissionPending;
      const stillSelected = state.card === dismissedCard;
      const detail = error instanceof Error ? error.message : String(error);
      if (submissionFailureKind(error) === "definitely-not-filed") {
        dismissedCard.dataset.dismissalSubmissionError = detail;
        if (stillSelected) {
          state.dismissing = false;
          state.filed = false;
          renderCompanion();
        }
      } else {
        dismissedCard.dataset.dismissalSubmissionUncertain = detail;
        delete dismissedCard.dataset.dismissalSubmissionError;
        if (stillSelected) {
          state.dismissing = false;
          state.filed = true;
          renderCompanion();
        }
      }
      console.error(error);
      return;
    }

    delete dismissedCard.dataset.dismissalSubmissionPending;
    delete dismissedCard.dataset.dismissalSubmissionError;
    delete dismissedCard.dataset.dismissalSubmissionUncertain;
    const stillSelected = state.card === dismissedCard;
    const clearWarning = hasOtherAwaitingBriefForIssue(
      allCards(),
      dismissedCard,
      issue,
    )
      ? ""
      : clearDraft(issue, "The brief was dismissed");
    archiveCurrentCard(dismissedCard, acknowledgement, stillSelected);
    if (stillSelected) {
      state.filed = true;
      state.dismissing = false;
      state.notice =
        `Brief dismissed without filing a ruling or waking an agent.${clearWarning}`;
      state.noticeKind = "acknowledged";
      renderCompanion();
    } else if (clearWarning) {
      console.error(clearWarning.trim());
    }
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
      const response = await trackedFetch(() =>
        fetch("/ruling", {
          method: "POST",
          headers: {"Content-Type": "application/json", Accept: "application/json"},
          credentials: "same-origin",
          body: JSON.stringify(payload),
        }),
      );
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
    const clearWarning = clearDraft(submittedIssue, "The ruling was filed");
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

  async function shareCurrentBrief() {
    if (state.sharing || state.revokingShare || !state.card) return;
    const sharedCard = state.card;
    state.sharing = true;
    setShareButtonState("SHARING…", true);
    hideShareResult();
    try {
      const response = await trackedFetch(() =>
        fetch("/shares", {
          method: "POST",
          headers: {"Content-Type": "application/json", Accept: "application/json"},
          credentials: "same-origin",
          body: JSON.stringify({page: sharedCard.dataset.briefName || ""}),
        }),
      );
      const record = await readShareResponse(response);
      shareByCard.set(sharedCard, record);
      if (state.card === sharedCard) {
        showShareRecord(record, false);
        const copied = await copyShareLink();
        showShareRecord(record, copied);
      }
    } catch (error) {
      if (state.card === sharedCard) {
        const detail = error instanceof Error ? error.message : String(error);
        showShareError(detail);
      }
      console.error(error);
    } finally {
      state.sharing = false;
      setShareButtonState("SHARE", state.revokingShare || !state.card);
    }
  }

  async function revokeCurrentShare() {
    if (state.sharing || state.revokingShare || !state.card) return;
    const sharedCard = state.card;
    const record = shareByCard.get(sharedCard);
    if (!record) return;
    state.revokingShare = true;
    setShareButtonState("REVOKING…", true);
    shareRevokeButton.disabled = true;
    try {
      const response = await trackedFetch(() =>
        fetch(
          `/shares/${encodeURIComponent(record.id)}/revoke`,
          {
            method: "POST",
            headers: {"Content-Type": "application/json", Accept: "application/json"},
            credentials: "same-origin",
            body: "{}",
          },
        ),
      );
      if (response.status !== 204) {
        let detail = "";
        try {
          const payload = await response.json();
          detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
        } catch (_error) {
          // The status remains the useful fallback.
        }
        throw new Error(detail || `the server returned HTTP ${response.status}`);
      }
      shareByCard.delete(sharedCard);
      if (state.card === sharedCard) {
        shareStatus.textContent = "Public link revoked";
        shareExpiry.textContent = "It is no longer available. Create a new link whenever needed.";
        shareLink.value = "";
        shareLink.parentElement.hidden = true;
        shareMarkdownLink.parentElement.hidden = true;
      }
    } catch (error) {
      if (state.card === sharedCard) {
        const detail = error instanceof Error ? error.message : String(error);
        showShareError(`Revocation failed: ${detail}`, record);
        shareByCard.set(sharedCard, record);
      }
      console.error(error);
    } finally {
      state.revokingShare = false;
      setShareButtonState("SHARE", state.sharing || !state.card);
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
  shareButton.addEventListener("click", () => void shareCurrentBrief());
  shareCopyButton.addEventListener("click", async () => {
    const copied = await copyShareLink();
    const record = state.card ? shareByCard.get(state.card) : null;
    if (record) showShareRecord(record, copied);
  });
  shareRevokeButton.addEventListener("click", () => void revokeCurrentShare());
  shareCloseButton.addEventListener("click", hideShareResult);
  reloadButton.addEventListener("click", () => window.location.reload());
  archiveDisclosure.addEventListener("click", () => {
    const expanded = archiveDisclosure.getAttribute("aria-expanded") === "true";
    archiveDisclosure.setAttribute("aria-expanded", String(!expanded));
    archivePanel.hidden = expanded;
  });
  phoneInboxButton.addEventListener("click", showPhoneInbox);
  dismissButton.addEventListener("click", () => void dismissCurrentBrief());
  sendButton.addEventListener("click", () => void fileRuling());
  ribbonDismissButton.addEventListener("click", () => void dismissCurrentBrief());
  ribbonSendButton.addEventListener("click", () => void fileRuling());
  frame.addEventListener("load", handleFrameLoad);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      foregroundAgeStartedAt = Date.now();
      return;
    }
    maybeReloadStaleInbox();
  });
  window.addEventListener("pageshow", maybeReloadStaleInbox);
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
