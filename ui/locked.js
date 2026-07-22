(() => {
  "use strict";

  // Authentication.issue_bootstrap_ticket currently uses
  // secrets.token_urlsafe(32), which yields 43 characters. Keep this broad
  // defensive bound aligned with server.py's BOOTSTRAP_TICKET grammar.
  const TICKET = /^[A-Za-z0-9_-]{32,256}$/;

  function inboxEnrollmentTicket(rawValue, currentOrigin) {
    if (typeof rawValue !== "string" || typeof currentOrigin !== "string") {
      return null;
    }
    let candidate;
    try {
      candidate = new URL(rawValue.trim());
    } catch (_) {
      return null;
    }
    if (
      candidate.origin !== currentOrigin ||
      candidate.username ||
      candidate.password ||
      candidate.pathname !== "/bootstrap" ||
      candidate.search
    ) {
      return null;
    }
    const parameters = new URLSearchParams(candidate.hash.slice(1));
    const keys = Array.from(parameters.keys());
    const tickets = parameters.getAll("ticket");
    if (
      keys.length !== 1 ||
      keys[0] !== "ticket" ||
      tickets.length !== 1 ||
      !TICKET.test(tickets[0])
    ) {
      return null;
    }
    return tickets[0];
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze({inboxEnrollmentTicket});
  }

  if (typeof document === "undefined") return;
  const form = document.querySelector("[data-enrollment-form]");
  if (!form) return;
  const input = form.querySelector("[data-enrollment-url]");
  const submit = form.querySelector("button[type=submit]");
  const status = form.querySelector("[data-enrollment-status]");
  if (!input || !submit || !status) return;

  function showStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ticket = inboxEnrollmentTicket(input.value, window.location.origin);
    input.value = "";
    if (!ticket) {
      showStatus(
        "Paste a fresh inbox enrollment link from this Arachne server, " +
          "not a link to an individual decision.",
        true,
      );
      input.focus();
      return;
    }

    input.disabled = true;
    submit.disabled = true;
    form.setAttribute("aria-busy", "true");
    showStatus("Unlocking Arachne…");
    try {
      const response = await fetch("/session", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "same-origin",
        body: JSON.stringify({ticket, page: "/"}),
      });
      if (!response.ok) {
        throw new Error("enrollment rejected");
      }
      window.location.replace("/");
    } catch (_) {
      input.disabled = false;
      submit.disabled = false;
      form.removeAttribute("aria-busy");
      showStatus(
        "That enrollment link is invalid, expired, or already used. Ask for a new one.",
        true,
      );
      input.focus();
    }
  });
})();
