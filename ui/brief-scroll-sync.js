/*
 * Arachne brief scroll-sync reporter.
 *
 * Copy this file verbatim into an inline script block in a decision brief.
 * The brief iframe has an opaque sandbox origin, so this block deliberately
 * has no external dependencies and coordinates with the chrome by postMessage.
 */
(() => {
  "use strict";

  const BRIEF_SOURCE = "arachne-brief";
  const CHROME_SOURCE = "arachne-chrome";
  let framePending = false;
  let lastReportedAxis = null;

  function hasExactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort();
    return (
      actual.length === expected.length &&
      expected.every((key, index) => actual[index] === key)
    );
  }

  function topmostVisibleAxis() {
    const anchors = document.querySelectorAll("[data-axis]");
    const viewportHeight =
      window.innerHeight || document.documentElement.clientHeight;
    let visible = null;
    let visibleTop = Number.POSITIVE_INFINITY;
    let nearest = null;
    let nearestDistance = Number.POSITIVE_INFINITY;

    for (const anchor of anchors) {
      const rect = anchor.getBoundingClientRect();
      if (rect.bottom > 0 && rect.top < viewportHeight) {
        if (rect.top < visibleTop) {
          visible = anchor;
          visibleTop = rect.top;
        }
        continue;
      }
      const distance = rect.top >= viewportHeight ? rect.top - viewportHeight : -rect.bottom;
      if (distance < nearestDistance) {
        nearest = anchor;
        nearestDistance = distance;
      }
    }
    return visible || nearest;
  }

  function reportInView() {
    framePending = false;
    const anchor = topmostVisibleAxis();
    const axis = anchor ? anchor.getAttribute("data-axis") : "";
    if (!axis || axis === lastReportedAxis) return;
    lastReportedAxis = axis;
    window.parent.postMessage(
      {source: BRIEF_SOURCE, type: "in-view", axis},
      "*",
    );
  }

  function scheduleReport() {
    if (framePending) return;
    framePending = true;
    window.requestAnimationFrame(reportInView);
  }

  window.addEventListener("scroll", scheduleReport, {passive: true});
  window.addEventListener("resize", scheduleReport);
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const data = event.data;
    if (
      hasExactKeys(data, ["source", "type"]) &&
      data.source === CHROME_SOURCE &&
      data.type === "request-in-view"
    ) {
      lastReportedAxis = null;
      reportInView();
      return;
    }
    if (!hasExactKeys(data, ["axis", "source", "type"])) return;
    if (
      data.source !== CHROME_SOURCE ||
      data.type !== "scroll-to" ||
      typeof data.axis !== "string"
    ) {
      return;
    }
    const anchor = Array.from(document.querySelectorAll("[data-axis]")).find(
      (candidate) => candidate.getAttribute("data-axis") === data.axis,
    );
    if (!anchor) {
      console.warn(
        `Arachne brief scroll sync could not find a data-axis anchor for "${data.axis}".`,
      );
      return;
    }
    anchor.scrollIntoView({behavior: "smooth", block: "start"});
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scheduleReport, {once: true});
  } else {
    scheduleReport();
  }
})();
