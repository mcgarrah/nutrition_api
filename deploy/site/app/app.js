/*
 * /app: camera barcode scan + name search, backed by GET /api/v1/lookup/{gtin}
 * and GET /api/v1/search (see SPECS.md).
 *
 * Barcode decoding is one of two engines depending on browser support:
 *   - window.BarcodeDetector (Chrome/Edge, Android) -- no library needed.
 *   - vendor/zxing.min.js (Safari/iOS fallback, BarcodeDetector is absent
 *     there as of this writing -- see MOBILE_APP.md "Camera access (PWA)").
 * Both are restricted to retail barcode formats only (EAN-13/EAN-8/UPC-A/
 * UPC-E), matching the API's accepted GTIN shapes (SPECS.md §1, §6).
 */
"use strict";

const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"];
const SCAN_INTERVAL_MS = 200;
const DEDUPE_MS = 2500; // ignore a repeat of the same code within this window
const SEARCH_DEBOUNCE_MS = 250;
const GTIN_PATTERN = /^(\d{8}|\d{12,14})$/; // mirrors app/core/lookup_routes.py

const video = document.getElementById("camera");
const scanStatus = document.getElementById("scanStatus");
const resultCard = document.getElementById("resultCard");
const resultBody = document.getElementById("resultBody");
const dismissBtn = document.getElementById("dismissBtn");
const tabScan = document.getElementById("tabScan");
const tabSearch = document.getElementById("tabSearch");
const panelScan = document.getElementById("panelScan");
const panelSearch = document.getElementById("panelSearch");
const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");

let stream = null;
let detector = null;
let zxingReader = null;
let scanning = false;
let lastCode = null;
let lastScanAt = 0;

// ── UPC-E -> UPC-A expansion ────────────────────────────────────────────
// Client-side, per MOBILE_APP.md's documented gap: the API's normalize_gtin
// only zero-pads, which is wrong for a compressed 6-digit code. Verified by
// hand against the standard reference example 0-425261-4 -> 042100005264
// (see comments inline).

function upcCheckDigit(elevenDigits) {
  let sumOdd = 0, sumEven = 0;
  for (let i = 0; i < 11; i++) {
    const n = Number(elevenDigits[i]);
    if (i % 2 === 0) sumOdd += n; else sumEven += n;
  }
  const total = sumOdd * 3 + sumEven;
  return String((10 - (total % 10)) % 10);
}

function expandUpcE(raw) {
  const digits = String(raw).replace(/\D/g, "");
  let ns, payload;
  if (digits.length === 8) {
    ns = digits[0];
    payload = digits.slice(1, 7); // supplied check digit (index 7) is ignored; recomputed below
  } else if (digits.length === 7) {
    ns = digits[0];
    payload = digits.slice(1, 7);
  } else if (digits.length === 6) {
    ns = "0"; // by far the common case for retail UPC-E
    payload = digits;
  } else {
    return digits; // not a recognizable UPC-E shape -- pass through as-is
  }

  const d = payload.split("");
  const last = d[5];
  let mid;
  // Standard UPC-E -> UPC-A expansion, keyed on the last payload digit.
  // Example: UPC-E payload "425261" (ns "0") -> UPC-A "042100005264".
  if (last === "0" || last === "1" || last === "2") {
    mid = d[0] + d[1] + last + "0000" + d[2] + d[3] + d[4];
  } else if (last === "3") {
    mid = d[0] + d[1] + d[2] + "00000" + d[3] + d[4];
  } else if (last === "4") {
    mid = d[0] + d[1] + d[2] + d[3] + "00000" + d[4];
  } else {
    mid = d[0] + d[1] + d[2] + d[3] + d[4] + "0000" + last;
  }
  const eleven = ns + mid;
  return eleven + upcCheckDigit(eleven);
}

function normalizeDecoded(rawValue, format) {
  if (format === "upc_e") return expandUpcE(rawValue);
  return String(rawValue).replace(/\D/g, "");
}

// ── Camera + decode loop ────────────────────────────────────────────────

async function startCamera() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } },
      audio: false,
    });
  } catch (err) {
    showCameraError(err);
    return;
  }
  video.srcObject = stream;
  await video.play();
  scanning = true;
  scanStatus.textContent = "Point the camera at a barcode";

  if (window.BarcodeDetector) {
    await startBarcodeDetectorLoop();
  } else {
    startZXingLoop();
  }
}

function showCameraError(err) {
  const name = err && err.name;
  let msg = "Couldn't access the camera.";
  if (name === "NotAllowedError") {
    msg = "Camera permission was denied. Allow camera access, or use Search instead.";
  } else if (name === "NotFoundError") {
    msg = "No camera found on this device. Use Search instead.";
  }
  scanStatus.textContent = msg;
}

async function startBarcodeDetectorLoop() {
  let formats = FORMATS;
  try {
    const supported = await BarcodeDetector.getSupportedFormats();
    formats = FORMATS.filter((f) => supported.includes(f));
    if (!formats.length) formats = FORMATS;
  } catch (err) {
    // getSupportedFormats() itself is unsupported on some implementations;
    // fall back to requesting all of FORMATS directly.
  }
  detector = new BarcodeDetector({ formats });

  const tick = async () => {
    if (!video.srcObject) return; // camera was stopped
    try {
      const results = await detector.detect(video);
      if (scanning && results.length) {
        handleDecode(results[0].rawValue, results[0].format);
      }
    } catch (err) {
      // A transient bad frame throws sometimes; ignore and keep polling.
    }
    setTimeout(tick, SCAN_INTERVAL_MS);
  };
  tick();
}

function zxingFormatName(fmt) {
  const name = ZXing.BarcodeFormat[fmt];
  return name ? name.toLowerCase() : null;
}

function startZXingLoop() {
  const hints = new Map();
  hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
    ZXing.BarcodeFormat.EAN_13, ZXing.BarcodeFormat.EAN_8,
    ZXing.BarcodeFormat.UPC_A, ZXing.BarcodeFormat.UPC_E,
  ]);
  zxingReader = new ZXing.BrowserMultiFormatReader(hints);
  zxingReader.decodeFromStream(stream, video, (result, err) => {
    if (!scanning || !result) return;
    handleDecode(result.getText(), zxingFormatName(result.getBarcodeFormat()));
  });
}

function stopCamera() {
  scanning = false;
  if (zxingReader) {
    zxingReader.reset();
    zxingReader = null;
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }
  video.srcObject = null;
}

function handleDecode(rawValue, format) {
  if (!scanning) return;
  const now = Date.now();
  if (rawValue === lastCode && now - lastScanAt < DEDUPE_MS) return;
  lastCode = rawValue;
  lastScanAt = now;
  scanning = false;
  const gtin = normalizeDecoded(rawValue, format);
  scanStatus.textContent = `Scanned ${gtin}`;
  lookupAndRender(gtin);
}

dismissBtn.addEventListener("click", () => {
  resultCard.hidden = true;
  if (!panelScan.hidden) {
    scanStatus.textContent = "Point the camera at a barcode";
    scanning = true;
  }
});

// ── Lookup + rendering (shared by scan and search) ──────────────────────

const NUTRIENT_ROWS = [
  ["calories_kcal", "Calories"],
  ["protein", "Protein"],
  ["fat", "Fat"],
  ["carbohydrates", "Carbohydrates"],
  ["sugars", "Sugars"],
  ["fiber", "Fiber"],
  ["sodium", "Sodium"],
];

function fmtNutrient(value) {
  if (value == null) return null;
  if (typeof value === "number") return String(value); // calories_kcal is a bare number
  return `${value.value}${value.unit}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderProduct(product) {
  const rows = NUTRIENT_ROWS
    .map(([key, label]) => [label, fmtNutrient(product[key])])
    .filter(([, value]) => value != null);

  const allergens = (product.allergens || []).map(escapeHtml).join(", ");

  resultBody.innerHTML = `
    ${product.image_url ? `<img class="product-image" src="${escapeHtml(product.image_url)}" alt="">` : ""}
    <h2>${escapeHtml(product.product_name || "Unknown product")}</h2>
    ${product.brand ? `<p class="brand">${escapeHtml(product.brand)}</p>` : ""}
    <dl class="nutrients">
      ${rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}
    </dl>
    ${allergens ? `<p class="allergens"><strong>Allergens:</strong> ${allergens}</p>` : ""}
  `;
  resultCard.hidden = false;
}

function renderNotFound(gtin) {
  resultBody.innerHTML = `
    <h2>Not found</h2>
    <p>No source has data for <code>${escapeHtml(gtin)}</code>.</p>
    <p>Try <button type="button" id="trysearchBtn" class="link-button">searching by name</button> instead.</p>
  `;
  resultCard.hidden = false;
  document.getElementById("trysearchBtn").addEventListener("click", () => {
    resultCard.hidden = true;
    switchTab("search");
  });
}

function renderLookupError() {
  resultBody.innerHTML = `
    <h2>Something went wrong</h2>
    <p>Couldn't reach the lookup service. Check your connection and try again.</p>
  `;
  resultCard.hidden = false;
}

async function lookupAndRender(gtin) {
  resultBody.innerHTML = `<p>Looking up ${escapeHtml(gtin)}…</p>`;
  resultCard.hidden = false;
  try {
    const resp = await fetch(`/api/v1/lookup/${encodeURIComponent(gtin)}`);
    if (resp.status === 404) {
      renderNotFound(gtin);
      return;
    }
    if (!resp.ok) {
      renderLookupError();
      return;
    }
    renderProduct(await resp.json());
  } catch (err) {
    renderLookupError();
  }
}

// ── Search ───────────────────────────────────────────────────────────

let searchTimer = null;

function renderSearchResults(results) {
  if (!results.length) {
    searchResults.innerHTML = `<p class="muted">No matches in the local copies.</p>`;
    return;
  }
  searchResults.innerHTML = results.map((r) => `
    <button type="button" class="search-result" data-gtin="${escapeHtml(r.gtin)}">
      ${r.image_url ? `<img src="${escapeHtml(r.image_url)}" alt="">` : `<span class="thumb-placeholder"></span>`}
      <span class="sr-text">
        <span class="sr-name">${escapeHtml(r.product_name || "Unknown")}</span>
        ${r.brand ? `<span class="sr-brand">${escapeHtml(r.brand)}</span>` : ""}
      </span>
    </button>
  `).join("");

  searchResults.querySelectorAll(".search-result").forEach((btn) => {
    btn.addEventListener("click", () => lookupAndRender(btn.dataset.gtin));
  });
}

async function runSearch(query) {
  // A pure barcode typed/pasted here is a lookup, not a name search -- useful
  // as a manual-entry fallback when the camera is unavailable or denied.
  if (GTIN_PATTERN.test(query)) {
    searchResults.innerHTML = "";
    lookupAndRender(query);
    return;
  }
  if (query.length < 2) {
    searchResults.innerHTML = "";
    return;
  }
  try {
    const resp = await fetch(`/api/v1/search?q=${encodeURIComponent(query)}&limit=20`);
    if (!resp.ok) return;
    const data = await resp.json();
    renderSearchResults(data.results || []);
  } catch (err) {
    searchResults.innerHTML = `<p class="muted">Search unavailable right now.</p>`;
  }
}

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  searchTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
});

// ── Tabs ─────────────────────────────────────────────────────────────

function switchTab(tab) {
  const scanActive = tab === "scan";
  tabScan.classList.toggle("active", scanActive);
  tabSearch.classList.toggle("active", !scanActive);
  panelScan.hidden = !scanActive;
  panelSearch.hidden = scanActive;
  resultCard.hidden = true;

  if (scanActive) {
    // Re-acquire the camera if Search released it; otherwise just resume.
    if (stream) scanning = true; else startCamera();
  } else {
    // Release the camera while on Search -- no reason to keep it running
    // (and draining battery) while the user is typing.
    stopCamera();
  }
}

tabScan.addEventListener("click", () => switchTab("scan"));
tabSearch.addEventListener("click", () => switchTab("search"));

// ── Install prompt ───────────────────────────────────────────────────
// Android/Chrome fires beforeinstallprompt and lets us trigger the native
// install dialog programmatically. Safari/iOS has no such API -- the only
// path is Share > Add to Home Screen, so there installBtn just opens
// instructions instead of a real prompt.

const installBtn = document.getElementById("installBtn");
const iosInstallSheet = document.getElementById("iosInstallSheet");
const iosInstallDismiss = document.getElementById("iosInstallDismiss");

const isStandalone = window.matchMedia("(display-mode: standalone)").matches
  || window.navigator.standalone === true; // iOS's own pre-standard flag
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;

let deferredInstallPrompt = null;

if (!isStandalone && isIOS) {
  // No install-readiness event exists to wait for on iOS -- show the button
  // up front; nothing here is asynchronous the way beforeinstallprompt is.
  installBtn.hidden = false;
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault(); // hold the browser's own mini-infobar; installBtn drives it instead
  deferredInstallPrompt = event;
  installBtn.hidden = false;
});

installBtn.addEventListener("click", async () => {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    installBtn.hidden = true;
    return;
  }
  if (isIOS) {
    iosInstallSheet.hidden = false;
  }
});

iosInstallDismiss.addEventListener("click", () => {
  iosInstallSheet.hidden = true;
});

window.addEventListener("appinstalled", () => {
  installBtn.hidden = true;
  deferredInstallPrompt = null;
});

// ── Startup ──────────────────────────────────────────────────────────

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/app/sw.js").catch(() => {
    // Offline support is a nice-to-have, not a hard requirement -- ignore.
  });
}

if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
  startCamera();
} else {
  showCameraError({ name: "NotFoundError" });
}
