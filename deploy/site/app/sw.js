/*
 * Service worker for /app, the in-store scanning PWA.
 *
 * Two caches, two different reasons to have them:
 *   - The app shell (this file's own assets) is cache-first, so /app loads
 *     reliably on flaky store wifi/cell signal before any network round
 *     trip completes.
 *   - GET /api/v1/lookup/{gtin} is network-first with a short timeout,
 *     falling back to whatever was cached for that exact barcode -- nutrient
 *     data can change, so a fresh answer is preferred, but a barcode looked
 *     up earlier (this trip or a prior one) still resolves offline.
 * GET /api/v1/search is deliberately left alone (network-only): a live
 * typeahead against a stale cached result set isn't useful, and search
 * requires connectivity anyway.
 *
 * Bump SHELL_CACHE's version suffix whenever a shell asset changes -- the
 * old cache is purged in activate(), same "don't serve a stale shell
 * forever" concern the rest of the site handles with a Cache-Control:
 * no-cache header (see caddy/site.caddy); this is that layer's equivalent
 * once a service worker is controlling /app.
 */
const SHELL_CACHE = "nutrition-app-shell-v2";
const LOOKUP_CACHE = "nutrition-app-lookup-v1";
const CURRENT_CACHES = [SHELL_CACHE, LOOKUP_CACHE];

const SHELL_ASSETS = [
  "/app/",
  "/app/index.html",
  "/app/app.js",
  "/app/manifest.json",
  "/app/vendor/zxing.min.js",
  "/app/icons/icon-192.png",
  "/app/icons/icon-512.png",
];

// GET /api/v1/lookup/{gtin} must answer within this long before falling back
// to whatever's cached -- long enough for a genuine cold lookup (upstream
// calls are capped at 2.0s each, run concurrently -- see SPECS.md §4), short
// enough that a dead connection doesn't leave the scanner hanging.
const LOOKUP_NETWORK_TIMEOUT_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => !CURRENT_CACHES.includes(name))
          .map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}

async function networkFirstLookup(request) {
  const cache = await caches.open(LOOKUP_CACHE);
  try {
    const response = await withTimeout(fetch(request), LOOKUP_NETWORK_TIMEOUT_MS);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirstShell(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  if (url.pathname.startsWith("/api/v1/lookup/")) {
    event.respondWith(networkFirstLookup(event.request));
    return;
  }

  if (url.pathname.startsWith("/api/v1/search")) {
    return; // network-only: don't intercept
  }

  if (url.pathname.startsWith("/app/")) {
    event.respondWith(cacheFirstShell(event.request));
  }
});
