# MOBILE_APP.md - API readiness for a grocery-store scanning app

Review of whether this API supports a phone app that scans barcodes with the
camera and falls back to name search, for use live in a grocery store.
Reviewed 2026-07-19. Not a spec — see [SPECS.md](SPECS.md) for the endpoint
contracts referenced here, and [PLAN.md](PLAN.md) for what's shipped/pending
on the API itself.

**Bottom line:** the API already fits this use case well. One real
client-side gap (UPC-E), everything else below is "know this going in," not
"fix this first."

## What already fits well

- **`GET /api/v1/lookup/{gtin}`** accepts GTIN-8/12/13/14
  (`^(\d{8}|\d{12,14})$`, `app/core/lookup_routes.py:17`) — EAN-8, UPC-A,
  EAN-13, GTIN-14. Exactly what camera scanning SDKs (ZXing, Google ML Kit,
  Apple VisionKit) emit for the barcode formats on grocery shelves. One call
  returns everything a product screen needs — name, brand, category, the
  full nutrient panel (standardized to per-100g/mL, so a field never changes
  unit with its source), image, ingredients, allergens. No second round trip
  per item.
- **`GET /api/v1/search`** is FTS5-backed prefix search over the local name
  mirrors (`app/core/search.py`) — fast enough for live typeahead as someone
  types a product name in the store, not just submit-and-wait.
- **No auth required.** Public, read-only GET API (`allow_methods=["GET"]`,
  `app/main.py:107`) — nothing to embed or protect client-side.
- **Fast for the common case.** The local mirrors cover 442K FDC barcodes and
  2.24M OFF products, so most real grocery items resolve in microseconds
  without touching the network. A miss falls through to live upstreams
  concurrently (`asyncio.gather`, `app/core/orchestrator.py:349`), capped at
  2s per source — worst case is USDA's two-round-trip path (~4s ceiling),
  not the common path.
- **404 vs 422 are distinguishable** — a genuinely unknown barcode (common
  for store-brand items) is a clean 404; a garbled scan is 422. Have the app
  fall back to name search on a 404 — a natural pairing since both are being
  built anyway.

## One real gap: UPC-E

Compressed 6-digit UPC-E codes (small packages — candy, gum, small bottles)
aren't expanded server-side; `normalize_gtin` just zero-pads whatever it's
given (`app/core/usda_fdc.py:173`), which is wrong for a compressed code.
Most scanning SDKs (ML Kit, AVFoundation) can either be told to skip UPC-E or
will expand it to UPC-A themselves — **expand it client-side before calling
the API** rather than sending the raw 6-digit code. Not planned as a
server-side fix; this belongs in the scanning layer, not the API.

## Things to know before building, not blockers

- **Reachability from outside the LAN.** The box is reachable via Tailscale
  Funnel's `*.ts.net` public URL (no custom domain yet — PLAN.md item 1 is
  shelved) and via direct tailnet/LAN access. For a phone actually in a
  grocery store, either install Tailscale on the phone (joins the tailnet,
  works even without Funnel) or hit the public Funnel HTTPS URL from any
  network.
- **Rate limit:** 60 req/min per IP, burst 20 (`SPECS.md` §5, `app/main.py`)
  — plenty for one person scanning a cart, but it's per-IP. Several users
  behind the same NAT (carrier CGNAT, campus wifi) would share a bucket. Not
  a concern for a personal app.
- **CORS is wide open on GET** (`allow_origins=["*"]`, `app/main.py:108`) —
  worth knowing because it means a **PWA** (camera via
  `getUserMedia`/`BarcodeDetector`, calling the API directly, "add to home
  screen") is a viable lighter-weight alternative to a native app if
  app-store friction isn't wanted. Not a recommendation, just an option this
  API doesn't block.
- **`GET /api/v1/health`** reports per-source degradation, never a hard
  error, so the app can show "results may be limited" instead of a scary
  failure if an upstream is down.

## Camera access (PWA)

A web app *can* scan barcodes with the phone's camera — no native app or app
store needed. `getUserMedia()` gets a live camera feed; decoding barcodes
from the frames is one of:

- **Native `BarcodeDetector` API** — built into the browser, no library.
  Supported in Chrome/Edge on Android and desktop, but **not in Safari on
  iOS** as of this review — a real gap for iPhone users.
- **A JS/WASM decoding library** (e.g. `@zxing/browser`) — processes frames
  in JS, works across all browsers including Safari/iOS. Slightly heavier,
  but the only reliable cross-platform option today.

Recommended approach: feature-detect `BarcodeDetector` and use it when
available (faster, less code), fall back to a JS library when it's not
(mainly Safari) — covers Android and iPhone shoppers without picking one
over the other.

Two things that matter for the "works in a grocery store" goal:

- **HTTPS is required** for camera access — not an issue here, since the API
  is already served over real TLS (Caddy + Tailscale Funnel), but the web
  app itself must also be served over HTTPS for camera permission to work.
- **iOS home-screen PWAs have had inconsistent camera-permission behavior**
  across iOS versions when installed to the home screen vs. opened in Safari
  directly — worth testing on an actual iPhone early rather than assuming
  parity with desktop Chrome.

## Status: shipped as a PWA at `/app`

The decisions this doc left open are made, and the app is built:

- **PWA, not native** — no app-store friction, one codebase for Android and
  iPhone, nothing in the API blocked it (open CORS, no auth).
- **Same repo, same site** — `deploy/site/app/` is picked up automatically
  by the existing Caddy static-file fallthrough (`deploy/caddy/site.caddy`'s
  `@backend` matcher is an explicit allowlist that doesn't include `/app`,
  so no Caddy config changes were needed at all). No new domain, no new TLS
  cert, no new Funnel setup.
- **Camera decode**: `BarcodeDetector` when available, falling back to a
  vendored `@zxing/library` build (`deploy/site/app/vendor/zxing.min.js`,
  not CDN-loaded, so the offline app shell is self-contained) for Safari/
  iOS. Both paths restricted to `ean_13`/`ean_8`/`upc_a`/`upc_e`.
- **The UPC-E gap is closed** — `deploy/site/app/app.js` expands a decoded
  UPC-E code to UPC-A client-side (`expandUpcE()`) before calling the API,
  verified by hand against the standard reference example
  (0-425261-4 → 042100005264).
- **Offline/caching strategy** (`deploy/site/app/sw.js`): the app shell is
  cache-first (loads reliably on flaky store wifi); `GET /api/v1/lookup/*`
  is network-first with a fallback to the last cached response for that
  barcode; `GET /api/v1/search*` is network-only, since a live typeahead
  against a stale result set isn't useful.
- **The vendored library is tracked, not just dropped in.** This project's
  `.github/dependabot.yml`/CI previously covered pip, GitHub Actions, and
  Docker only — deliberately no npm entry, because there was no real npm
  dependency surface until now. A root `package.json` declares
  `@zxing/library@0.21.3` so `npm audit` (both PR-blocking in `ci.yml` and
  the scheduled scan in `dependency-audit.yml`, mirroring the existing
  pip-audit pattern) and Dependabot can actually see it. A CI step also
  diffs the committed vendored file against a fresh `npm ci` install
  byte-for-byte, so the audited version and the deployed version can't
  silently drift apart.

**Not yet done: a real-device check.** Camera access can't be verified from
a dev container — no camera, no mobile browser. Before relying on this in a
store: open `/app` on a phone, grant camera permission, scan a real
barcode, confirm the result card renders, and try the Search tab (including
typing a bare barcode into the search box, which is treated as a lookup —
a manual-entry fallback for when the camera is denied or unavailable).
