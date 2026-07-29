"use strict";

const CACHE_PREFIX = "arachne-offline-";
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const OFFLINE_URL = "/offline.html";
const STARTUP_TIMEOUT_MS = 7000;

function isInboxNavigation(request, origin) {
  if (!request || request.method !== "GET" || request.mode !== "navigate") {
    return false;
  }
  try {
    const url = new URL(request.url);
    return url.origin === origin && url.pathname === "/";
  } catch (_) {
    return false;
  }
}

async function fetchInboxOrOffline(request, options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
  const matchImpl =
    options.matchImpl || ((url) => globalThis.caches.match(url));
  const AbortControllerImpl =
    options.AbortControllerImpl || globalThis.AbortController;
  const timeoutMs = options.timeoutMs ?? STARTUP_TIMEOUT_MS;
  const controller = new AbortControllerImpl();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(request, {signal: controller.signal});
    if (response.status < 500) return response;
  } catch (_) {
    // Network errors and the bounded startup timeout share the safe fallback.
  } finally {
    clearTimeout(timer);
  }

  try {
    const cached = await matchImpl(OFFLINE_URL);
    if (cached) return cached;
  } catch (_) {
    // A cache failure must still produce an explanatory response.
  }
  return new Response("Arachne cannot reach the live tailnet service.", {
    status: 503,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = Object.freeze({
    OFFLINE_URL,
    STARTUP_TIMEOUT_MS,
    fetchInboxOrOffline,
    isInboxNavigation,
  });
}

if (typeof self !== "undefined" && self.addEventListener) {
  self.addEventListener("install", (event) => {
    event.waitUntil(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        await cache.add(new Request(OFFLINE_URL, {cache: "reload"}));
        await self.skipWaiting();
      })(),
    );
  });

  self.addEventListener("activate", (event) => {
    event.waitUntil(
      (async () => {
        const names = await caches.keys();
        await Promise.all(
          names
            .filter(
              (name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME,
            )
            .map((name) => caches.delete(name)),
        );
        await self.clients.claim();
      })(),
    );
  });

  self.addEventListener("fetch", (event) => {
    if (!isInboxNavigation(event.request, self.location.origin)) return;
    event.respondWith(fetchInboxOrOffline(event.request));
  });
}
