/*
 * Arachne brief capture and scroll-sync agent.
 *
 * Copy this file verbatim into an inline script block in a decision brief.
 * The brief iframe has an opaque sandbox origin, so this block deliberately
 * has no external dependencies and coordinates with the chrome by postMessage.
 * data-answered and arachneCaptureHooks.isAnswered are trusted brief-side
 * completeness overrides: a brief that uses either is responsible for not
 * asserting that a meaningless ruling is complete.
 */
(() => {
  "use strict";

  const BRIEF_SOURCE = "arachne-brief";
  const CHROME_SOURCE = "arachne-chrome";
  const CONTROL_SELECTOR = "input[name], select[name], textarea[name]";

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

  function normalizedTag(control) {
    return String(control?.tagName || "").toLowerCase();
  }

  function normalizedType(control) {
    return String(control?.type || "").toLowerCase();
  }

  function namedControls(root) {
    if (root && typeof root.querySelectorAll === "function") {
      return Array.from(root.querySelectorAll(CONTROL_SELECTOR));
    }
    if (root && typeof root[Symbol.iterator] === "function") {
      return Array.from(root);
    }
    return [];
  }

  function selectedValues(control) {
    const tag = normalizedTag(control);
    const type = normalizedType(control);
    if (tag === "input" && (type === "radio" || type === "checkbox")) {
      return control.checked ? [String(control.value ?? "on")] : [];
    }
    if (tag === "select" && control.multiple) {
      const options = control.selectedOptions || control.options || [];
      return Array.from(options)
        .filter((option) => option.selected !== false)
        .map((option) => String(option.value ?? ""));
    }
    return [String(control.value ?? "")];
  }

  function serializeForm(root) {
    const groups = new Map();
    for (const control of namedControls(root)) {
      const name = String(control?.name || "");
      if (!name) continue;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(control);
    }

    const form = {};
    for (const [name, controls] of groups) {
      const values = controls.flatMap(selectedValues);
      const allRadios = controls.every(
        (control) =>
          normalizedTag(control) === "input" && normalizedType(control) === "radio",
      );
      const multiple =
        controls.some(
          (control) =>
            normalizedTag(control) === "input" &&
            normalizedType(control) === "checkbox",
        ) ||
        controls.some(
          (control) => normalizedTag(control) === "select" && control.multiple,
        ) ||
        (!allRadios && controls.length > 1);
      Object.defineProperty(form, name, {
        value: multiple ? values : values[0] ?? "",
        enumerable: true,
        configurable: true,
        writable: true,
      });
    }
    return form;
  }

  function partNames(part) {
    if (Array.isArray(part?.names)) return part.names;
    if (part?.root) {
      return Array.from(
        new Set(
          namedControls(part.root)
            .map((control) => String(control?.name || ""))
            .filter(Boolean),
        ),
      );
    }
    return [];
  }

  function nonEmptyValues(value) {
    const values = Array.isArray(value) ? value : [value];
    return values
      .filter((candidate) => candidate !== null && candidate !== undefined)
      .map((candidate) => String(candidate).trim())
      .filter(Boolean);
  }

  function composeMarkdown(form, parts) {
    return parts
      .map((part) => {
        const values = partNames(part).flatMap((name) =>
          Object.prototype.hasOwnProperty.call(form, name)
            ? nonEmptyValues(form[name])
            : [],
        );
        const emptyMarker = part.answered ? "(no value)" : "— (unanswered)";
        return `${part.label}: ${values.length ? values.join(", ") : emptyMarker}`;
      })
      .join("\n");
  }

  function isValidParentMessage(data) {
    if (
      hasExactKeys(data, ["source", "type"]) &&
      data.source === CHROME_SOURCE &&
      data.type === "request-in-view"
    ) {
      return true;
    }
    if (
      hasExactKeys(data, ["axis", "source", "type"]) &&
      data.source === CHROME_SOURCE &&
      data.type === "scroll-to" &&
      typeof data.axis === "string"
    ) {
      return true;
    }
    if (
      hasExactKeys(data, ["source", "token", "type"]) &&
      data.source === CHROME_SOURCE &&
      data.type === "collect" &&
      typeof data.token === "string" &&
      data.token.length > 0
    ) {
      return true;
    }
    return (
      hasExactKeys(data, ["form", "source", "type"]) &&
      data.source === CHROME_SOURCE &&
      data.type === "restore" &&
      isPlainObject(data.form)
    );
  }

  function makeRulingMessage(token, form, markdown, allAnswered) {
    return {
      source: BRIEF_SOURCE,
      type: "ruling",
      token,
      form,
      markdown,
      allAnswered,
    };
  }

  function restoreForm(root, form) {
    const groups = new Map();
    for (const control of namedControls(root)) {
      const name = String(control?.name || "");
      if (!name) continue;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(control);
    }

    for (const [name, controls] of groups) {
      if (!Object.prototype.hasOwnProperty.call(form, name)) continue;
      const saved = form[name];
      const values = (Array.isArray(saved) ? saved : [saved]).map((value) =>
        String(value ?? ""),
      );
      let ordinaryIndex = 0;
      for (const control of controls) {
        const tag = normalizedTag(control);
        const type = normalizedType(control);
        if (tag === "input" && (type === "radio" || type === "checkbox")) {
          control.checked = values.includes(String(control.value ?? "on"));
          continue;
        }
        if (tag === "select" && control.multiple) {
          for (const option of Array.from(control.options || [])) {
            option.selected = values.includes(String(option.value ?? ""));
          }
          continue;
        }
        control.value = Array.isArray(saved)
          ? values[ordinaryIndex] ?? ""
          : values[0] ?? "";
        ordinaryIndex += 1;
      }
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze({
      composeMarkdown,
      isValidParentMessage,
      makeRulingMessage,
      restoreForm,
      serializeForm,
    });
  }

  if (typeof window === "undefined" || typeof document === "undefined") return;

  let captureFramePending = false;
  let scrollFramePending = false;
  let lastReportedAxis = null;

  function anchorId(anchor) {
    if (anchor.hasAttribute("data-decision")) {
      return anchor.getAttribute("data-decision") || "";
    }
    return anchor.getAttribute("data-axis") || "";
  }

  function scrollAnchors() {
    return Array.from(document.querySelectorAll("[data-decision], [data-axis]"));
  }

  function topmostVisibleAnchor() {
    const viewportHeight =
      window.innerHeight || document.documentElement.clientHeight;
    let visible = null;
    let visibleTop = Number.POSITIVE_INFINITY;
    let nearest = null;
    let nearestDistance = Number.POSITIVE_INFINITY;

    for (const anchor of scrollAnchors()) {
      const rect = anchor.getBoundingClientRect();
      if (rect.bottom > 0 && rect.top < viewportHeight) {
        if (rect.top < visibleTop) {
          visible = anchor;
          visibleTop = rect.top;
        }
        continue;
      }
      const distance =
        rect.top >= viewportHeight ? rect.top - viewportHeight : -rect.bottom;
      if (distance < nearestDistance) {
        nearest = anchor;
        nearestDistance = distance;
      }
    }
    return visible || nearest;
  }

  function reportInView() {
    scrollFramePending = false;
    const anchor = topmostVisibleAnchor();
    const axis = anchor ? anchorId(anchor) : "";
    if (!axis || axis === lastReportedAxis) return;
    lastReportedAxis = axis;
    window.parent.postMessage(
      {source: BRIEF_SOURCE, type: "in-view", axis},
      "*",
    );
  }

  function scheduleInView() {
    if (scrollFramePending) return;
    scrollFramePending = true;
    window.requestAnimationFrame(reportInView);
  }

  function partLabel(element) {
    const declared = (element.getAttribute("data-label") || "").trim();
    if (declared) return declared;
    const heading = element.querySelector("h1, h2, h3, h4, h5, h6");
    const text = (heading?.textContent || element.textContent || "").trim();
    return text.replace(/\s+/g, " ");
  }

  function controlHasValue(control) {
    const tag = normalizedTag(control);
    const type = normalizedType(control);
    if (tag === "input" && (type === "radio" || type === "checkbox")) {
      return Boolean(control.checked);
    }
    if (tag === "input" && ["button", "hidden", "image", "reset", "submit"].includes(type)) {
      return false;
    }
    if (tag === "select" && control.multiple) {
      return selectedValues(control).some((value) => value.trim());
    }
    return String(control.value ?? "").trim().length > 0;
  }

  function partAnswered(element, root) {
    const declared = element.getAttribute("data-answered");
    if (declared === "true") return true;
    if (declared === "false") return false;
    const hook = window.arachneCaptureHooks?.isAnswered;
    if (typeof hook === "function") {
      const answer = hook(element, root);
      if (typeof answer === "boolean") return answer;
    }
    return namedControls(element).some(controlHasValue);
  }

  function captureParts() {
    return Array.from(document.querySelectorAll("[data-decision]")).map(
      (element) => ({
        id: element.getAttribute("data-decision") || "",
        label: partLabel(element),
        answered: partAnswered(element, document),
        names: Array.from(
          new Set(
            namedControls(element)
              .map((control) => String(control?.name || ""))
              .filter(Boolean),
          ),
        ),
        root: element,
      }),
    );
  }

  function collectCaptureState() {
    const internalParts = captureParts();
    const parts = internalParts.map(({id, label, answered}) => ({
      id,
      label,
      answered,
    }));
    const form = serializeForm(document);
    const compose = window.arachneCaptureHooks?.composeMarkdown;
    let markdown;
    if (typeof compose === "function") {
      markdown = compose(form, parts);
      if (typeof markdown !== "string") {
        throw new TypeError(
          "arachneCaptureHooks.composeMarkdown(form, parts) must return a string",
        );
      }
    } else {
      markdown = composeMarkdown(form, internalParts);
    }
    const issue =
      document.documentElement.getAttribute("data-issue") ||
      document.body?.getAttribute("data-issue") ||
      "";
    return {
      issue,
      parts,
      allAnswered:
        parts.length > 0 && parts.every((part) => part.answered),
      form,
      markdown,
    };
  }

  function reportCapture() {
    captureFramePending = false;
    const capture = collectCaptureState();
    window.parent.postMessage(
      {
        source: BRIEF_SOURCE,
        type: "capture",
        issue: capture.issue,
        parts: capture.parts,
        allAnswered: capture.allAnswered,
        form: capture.form,
        markdown: capture.markdown,
      },
      "*",
    );
  }

  function reportRuling(token) {
    const capture = collectCaptureState();
    window.parent.postMessage(
      makeRulingMessage(
        token,
        capture.form,
        capture.markdown,
        capture.allAnswered,
      ),
      "*",
    );
  }

  function scheduleCapture() {
    if (captureFramePending) return;
    captureFramePending = true;
    window.requestAnimationFrame(reportCapture);
  }

  window.addEventListener("scroll", scheduleInView, {passive: true});
  window.addEventListener("resize", scheduleInView);
  document.addEventListener("input", scheduleCapture);
  document.addEventListener("change", scheduleCapture);
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || !isValidParentMessage(event.data)) return;
    const data = event.data;
    if (data.type === "collect") {
      reportRuling(data.token);
      return;
    }
    if (data.type === "request-in-view") {
      lastReportedAxis = null;
      reportInView();
      reportCapture();
      return;
    }
    if (data.type === "restore") {
      restoreForm(document, data.form);
      scheduleCapture();
      return;
    }
    const anchor = scrollAnchors().find(
      (candidate) => anchorId(candidate) === data.axis,
    );
    if (!anchor) {
      console.warn(
        `Arachne brief agent could not find a decision part or data-axis anchor for "${data.axis}".`,
      );
      return;
    }
    anchor.scrollIntoView({behavior: "smooth", block: "start"});
  });

  function scheduleInitialReports() {
    scheduleInView();
    scheduleCapture();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scheduleInitialReports, {
      once: true,
    });
  } else {
    scheduleInitialReports();
  }
  window.addEventListener("load", scheduleInitialReports, {once: true});
})();
