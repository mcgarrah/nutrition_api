// Tests for the inline JavaScript in the static pages.
//
// These pages get no coverage from pytest, which is how a duplicate `const u`
// — a SyntaxError that froze the status dashboard on "Checking…" — shipped
// unnoticed. Node's built-in test runner and `vm` module give real coverage
// with no npm dependency: every page's script is parse-checked (that alone
// catches the duplicate-const class of bug), and the status page's logic is
// unit- and integration-tested in a sandbox with DOM and network stubs.
//
// Run: node --test jstests/

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const noop = () => {};

const PAGES = [
  "app/static/index.html",
  "app/static/lookup.html",
  "app/static/data.html",
  "app/static/search.html",
  "deploy/site/index.html",
  "deploy/site/status.html",
  "deploy/site/nav.js",
];

// Pull the inline <script> bodies out of an HTML file (skipping <script src=…>).
function inlineScripts(html) {
  const out = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) out.push(m[1]);
  return out;
}

function pageScript(rel) {
  const src = fs.readFileSync(path.join(ROOT, rel), "utf8");
  if (rel.endsWith(".js")) return src;          // a plain script file, not HTML to extract from
  return inlineScripts(src).join("\n;\n");
}

// ── Every page's inline script must parse ─────────────────────────────
// new vm.Script() compiles without executing, so a SyntaxError (the duplicate
// `const`, an unclosed brace, a stray token) fails here — the guard that was
// missing when the status page broke.

for (const page of PAGES) {
  test(`${page}: inline script parses`, () => {
    const code = pageScript(page);
    assert.ok(code.trim().length > 0, "expected a non-empty inline script");
    assert.doesNotThrow(() => new vm.Script(code, { filename: page }));
  });
}

// ── Sandbox for the status page ───────────────────────────────────────
// Runs the page script with DOM and network stubs. Function declarations
// (cls, tile, fmtDate, …) become properties of the context, so they can be
// called directly; getElementById hands back a distinct fake element per id so
// a test can read what was rendered.

function evalStatus(fetchImpl) {
  const els = {};
  const makeEl = () => ({
    textContent: "", innerHTML: "", className: "",
    style: { setProperty: noop }, addEventListener: noop,
  });
  const ctx = {
    document: { getElementById: id => (els[id] = els[id] || makeEl()), addEventListener: noop },
    window: { addEventListener: noop },
    fetch: fetchImpl || (() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
    AbortController: function () { this.signal = {}; this.abort = noop; },
    setTimeout: () => 0, clearTimeout: noop, setInterval: noop,
    Promise, Date, console, Object, Array, JSON, Math, String, Number, RegExp,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  new vm.Script(pageScript("deploy/site/status.html")).runInContext(ctx);
  return { ctx, els };
}

// ── status page: pure helpers ─────────────────────────────────────────

test("status: cls maps a source status to a visual state", () => {
  const { ctx } = evalStatus();
  assert.strictEqual(ctx.cls("ok"), "s-ok");
  assert.strictEqual(ctx.cls("error"), "s-bad");
  assert.strictEqual(ctx.cls("degraded"), "s-warn");     // anything else → warn
  assert.strictEqual(ctx.cls("unconfigured"), "s-warn");
  assert.strictEqual(ctx.cls("unknown"), "s-unknown");
  assert.strictEqual(ctx.cls(null), "s-unknown");
});

test("status: fmtNum groups digits, tolerates non-numbers", () => {
  const { ctx } = evalStatus();
  assert.match(ctx.fmtNum(442095), /442.?095/);          // 442,095 (locale comma)
  assert.strictEqual(ctx.fmtNum(null), "—");
  assert.strictEqual(ctx.fmtNum(undefined), "—");
});

test("status: fmtDate takes the date part, tolerates empty", () => {
  const { ctx } = evalStatus();
  assert.strictEqual(ctx.fmtDate("2026-07-15T00:24:59+00:00"), "2026-07-15");
  assert.strictEqual(ctx.fmtDate(null), "—");
});

test("status: datasetDate pulls YYYY-MM-DD out of a dataset name", () => {
  const { ctx } = evalStatus();
  assert.strictEqual(
    ctx.datasetDate("FoodData_Central_branded_food_csv_2026-04-30"), "2026-04-30");
  assert.strictEqual(ctx.datasetDate("off-2026-07-14"), "2026-07-14");
  assert.strictEqual(ctx.datasetDate("no date here"), null);
  assert.strictEqual(ctx.datasetDate(null), null);
});

test("status: tile escapes its content and shows the status pill", () => {
  const { ctx } = evalStatus();
  const html = ctx.tile("USDA <x>", "ok", [["token", "real"]]);
  assert.match(html, /s-ok/);
  assert.match(html, /operational/);
  assert.match(html, /USDA &lt;x&gt;/);                  // escaped, no raw <x>
  assert.ok(!html.includes("<x>"));
});

test("status: group omits an empty section", () => {
  const { ctx } = evalStatus();
  assert.strictEqual(ctx.group("Edge", [null, false]), "");
  assert.match(ctx.group("Edge", ["<div>x</div>"]), /Edge/);
});

// ── status page: full render (integration) ────────────────────────────

const FAKE_HEALTH = {
  status: "ok",
  usda_fdc: { status: "ok", key: "demo", total_foods: 100 },
  open_food_facts: { status: "ok" },
  usda_fdc_local: { status: "ok", dataset: "FoodData_Central_branded_food_csv_2026-04-30",
    barcodes: 442095, imported_at: "2026-07-14T21:28:37+00:00", size_mb: 321.5 },
  open_food_facts_local: { status: "ok", dataset: "off-2026-07-14", products: 2241589,
    imported_at: "2026-07-15T00:24:59+00:00", size_mb: 1026 },
  gpc: { status: "ok", version: "20260520",
    counts: { segments: 1, families: 27, classes: 135, bricks: 879,
      attribute_types: 384, attribute_values: 6701 },
    scope: "Food/Beverage (1 of 44 GS1 segments)", size_mb: 1.73, xml_date: "20/5/2026" },
  response_store: { enabled: true, ttl_days: 30, records: { "off/product": 22 } },
};

function fakeFetch(health) {
  return (url) => Promise.resolve(
    url === "/caddy-health"
      ? { ok: true }
      : { ok: true, json: () => Promise.resolve(health) });
}

test("status: a healthy payload renders every group and a DEMO_KEY warning", async () => {
  const { ctx, els } = evalStatus(fakeFetch(FAKE_HEALTH));
  await ctx.load();
  const body = els.body.innerHTML;

  assert.match(body, /Caddy proxy/);
  assert.match(body, /GPC taxonomy/);
  assert.match(body, /879 brick/);                 // the hierarchy chain
  assert.match(body, /384 types \/ 6,?701 values/); // attributes
  assert.match(body, /DEMO_KEY/);                  // token flagged
  assert.match(body, /1\.73 MB/);                  // GPC db size
  // A DEMO_KEY that is otherwise "ok" must render as attention, not operational.
  assert.match(els.overall.textContent, /Degraded|attention/i);
});

test("status: a backend outage still renders (proxy up, backend down)", async () => {
  // caddy-health ok, but /api/v1/health rejects.
  const fetchImpl = (url) => url === "/caddy-health"
    ? Promise.resolve({ ok: true })
    : Promise.reject(new Error("connection refused"));
  const { ctx, els } = evalStatus(fetchImpl);
  await ctx.load();

  assert.match(els.body.innerHTML, /Caddy proxy/);          // proxy tile shown
  assert.match(els.body.innerHTML, /API backend/);          // backend tile shown
  assert.match(els.overall.textContent, /Outage|down/i);    // overall reflects it
});
