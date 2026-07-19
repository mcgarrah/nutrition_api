# SPECS.md - Technical Specifications

## 1. OpenAPI & API Contracts

### Endpoint: `GET /api/v1/lookup/{gtin}`
Retrieves a standardized, consolidated profile of a food product using its GTIN/UPC barcode string.

#### Path Parameters
- `gtin` (string, required): A numeric string matching standard barcode specifications (GTIN-8, GTIN-12, GTIN-13, or GTIN-14 — pattern `^(\d{8}|\d{12,14})$`). Malformed values are rejected with `422 Unprocessable Entity`.

#### Query Parameters
- `fresh` (bool, default `false`): bypass every cache layer — the in-memory cache, the local bulk mirrors, and the disk store — and query the live upstream APIs. Slower, and spends rate-limit budget, but returns the newest data the upstreams have. The result refreshes the caches for subsequent requests.

#### Response Contracts (`200 OK`)
```json
{
  "gtin": "04963406021372",
  "product_name": "Coca-Cola Classic",
  "brand": "The Coca-Cola Company",
  "category_hierarchy": [
    "Food/Beverage/Tobacco",
    "Beverages",
    "Carbonated Soft Drinks",
    "Cola Drinks"
  ],
  "category_hierarchy_source": "fdc_curated",
  "calories_kcal": 42.0,
  "protein": { "value": 0.0, "unit": "g" },
  "fat": { "value": 0.0, "unit": "g" },
  "carbohydrates": { "value": 10.6, "unit": "g" },
  "fiber": null,
  "sugars": { "value": 10.6, "unit": "g" },
  "sodium": { "value": 4.0, "unit": "mg" },
  "caffeine": { "value": 9.6, "unit": "mg" },
  "vitamin_c": null,
  "image_url": "https://static.openfoodfacts.org/images/products/049/634/060/21372/front_en.400.jpg",
  "ingredients_text": "Carbonated water, high fructose corn syrup, caramel color, phosphoric acid, natural flavors, caffeine",
  "allergens": [],
  "labels": [],
  "data_sources": ["USDA_FDC", "OpenFoodFacts", "GS1_GPC"],
  "upstream_latency_ms": { "USDA_FDC": 110.5, "OpenFoodFacts": 95.2, "GS1_GPC": 5.1 },
  "cached": false,
  "provenance": {
    "USDA_FDC": { "origin": "local", "dataset": "fdc-2026-04-30", "dataset_date": "2026-04-30" },
    "OpenFoodFacts": { "origin": "live", "dataset": null, "dataset_date": null }
  },
  "attribution": { "OpenFoodFacts": { "licence": "ODbL 1.0", "url": "..." } }
}
```

The example elides most of the **36 nutrient fields** (the full US Nutrition
Facts label panel plus every vitamin and mineral `app/core/nutrients.py`
tracks); each is a `NutrientValue | null`, so "not reported" is
distinguishable from zero. Governance fields:

- `cached` — true when served from the in-memory cache without contacting any
  source. `upstream_latency_ms` always describes the fetch that *produced* the
  data, not the request that just returned it.
- `provenance` — per source, whether the answer came from the local bulk
  mirror (with its dataset date) or the live API.
- `attribution` — licence and attribution for each contributing source;
  Open Food Facts' ODbL *requires* attribution, so the notice travels with
  the data.

#### Error Responses
- `404 Not Found` — no upstream source has any data for the GTIN.
- `422 Unprocessable Entity` — the GTIN is not a valid numeric barcode string.
- `429 Too Many Requests` — the caller exceeded the inbound rate limit. Carries a `Retry-After` header (seconds).

### Endpoint: `GET /api/v1/search`
Search the **local FDC and OFF bulk mirrors** by product name. Returns identity fields only (`gtin`, `product_name`, `brand`, `image_url`, `source`) — enough to render a pick-list; the full merged panel for a chosen result comes from `/api/v1/lookup/{gtin}`, so exactly one place merges sources. Never falls through to a live upstream search. An empty `results` list means neither local copy matched — not that no upstream has the product.

- `q` (string, max length **200**): product name or substring.
- `limit` (int, 1–50, default 20).

### Secondary endpoint inventory

Full request/response shapes live in the OpenAPI schema (`/docs`); this table
is the map of what exists and why. All are `GET`, read-only.

| Endpoint group | Purpose |
| :--- | :--- |
| `/api/v1/usda/*` | Direct USDA FDC passthrough (search, food detail) — debugging and comparison against the merged view |
| `/api/v1/off/*` | Direct Open Food Facts passthrough — same purpose |
| `/api/v1/gpc/*` | GPC taxonomy browser API: drill Segment → Family → Class → Brick → Attributes, plus text search |
| `/api/v1/gpc/mappings` | Every curated FDC-category → GPC entry with resolved hierarchy, live coverage %, and the ranked uncovered-category list |
| `/api/v1/data/*` | Read-only Data Browser over the local stores (schema, rows, per-column coverage) |
| `/api/v1/health` | Per-source status; returns `degraded`, never an error, when an upstream is down |
| `/api/v1/version`, `/api/v1/attribution` | Build identity; licence terms per source |

Static UI pages (excluded from the OpenAPI schema): `/` (routing landing
page), `/lookup`, `/search`, `/data` (four tabs: Data Browser, Data Quality,
GPC Taxonomy, GPC Mappings -- `/gpc` and `/gpc/mappings` redirect here, see
PLAN.md item 11), `/app` -- the installable mobile PWA (camera barcode scan
+ name search), served as plain static files from `deploy/site/app/` with
no backend route of its own. See ARCH.md, "Mobile Scanner (PWA): Cross-
Browser Barcode Decode" for how it decodes a barcode across browsers, and
MOBILE_APP.md for the readiness review and real-device verification notes.

## 2. Pydantic Verification Schemas
Nutritional units are systematically mapped to a standardized baseline measurement of **100g** or **100mL** of product to eliminate arbitrary manufacturer portion definitions and ensure mathematical parity inside data science operations. Units are **ours, not the source's**: sodium is always mg whether USDA sent mg or Open Food Facts sent grams, so a field never changes unit with its provenance.

```python
class NutrientValue(BaseModel):
    value: float
    unit: str = "g"

class CanonicalProduct(BaseModel):
    gtin: str
    product_name: str = "Unknown"
    brand: str | None = None
    category_hierarchy: list[str] = []
    category_hierarchy_source: Literal["fdc_curated", "reviewed", "off_fuzzy", "none"] = "none"
    calories_kcal: float | None = None
    protein: NutrientValue | None = None
    fat: NutrientValue | None = None
    carbohydrates: NutrientValue | None = None
    fiber: NutrientValue | None = None
    sugars: NutrientValue | None = None
    sodium: NutrientValue | None = None
    # ...plus every vitamin and mineral app/core/nutrients.py tracks (36
    # nutrient fields in total) -- see app/core/models.py for the full set.
    image_url: str | None = None
    ingredients_text: str | None = None
    allergens: list[str] = []
    labels: list[str] = []
    data_sources: list[str] = []
    upstream_latency_ms: dict[str, float] = {}
    cached: bool = False
    provenance: dict[str, SourceProvenance] = {}
    attribution: dict[str, dict[str, str]] = {}
```

The authoritative definition lives in `app/core/models.py`.

## 3. Reconciliation Matrices & Ranked Truth Rules
When fields are provided concurrently across vendors, conflicts are evaluated deterministically using an embedded ingestion table:

| Objective Field | Authoritative Source | Validation Strategy / Transformation Rules |
| :--- | :--- | :--- |
| **Nutrients** | **USDA FDC** | Reconciles raw laboratory outputs; per-100g baselines. Open Food Facts values are provisional and overridden when USDA data exists. |
| **Media / Images** | **Open Food Facts** | Validates public URLs; grabs real-time label photography strings. |
| **Taxonomy** | **GS1 GPC** | Three tiers, graded by confidence and reported in `category_hierarchy_source`: FDC's own category resolves through a hand-curated mapping (`fdc_curated`) when one exists; else an OFF category tag resolves through its own hand-curated mapping (`reviewed`, same confidence); else OFF's tags are matched against GPC brick descriptions by best-effort text search (`off_fuzzy`). See ARCH.md, "GPC Category Matching". |
| **Ingredients** | **USDA FDC** | USDA's official label text overrides Open Food Facts' crowd-sourced text when USDA has an ingredients list; OFF's text is used when USDA has none. |

Within a single source, the **local bulk mirror answers before the live API**
(see ARCH.md, "Local Bulk Mirrors") — the mirror is the same data, just
earlier; the live API remains authoritative for anything newer than the
mirror's dataset date.

## 4. Resilience Contracts
- Every upstream call is capped at **2.0 seconds** (`UPSTREAM_TIMEOUT_S`).
- Per-source circuit breakers open after **5 consecutive failures** and skip the source for a **60s cooldown** before half-open probing.
- Successful lookups are cached in-memory for **300s** (`LOOKUP_CACHE_TTL_S`), max **1024 entries** (`LOOKUP_CACHE_MAX_SIZE`).
- Upstream failures never surface as `5xx`: the response degrades to a partial `200 OK` with the failed source absent from `data_sources`.

## 5. Rate Limits

**Inbound** — 60 requests/minute per client IP, burst 20 (`INBOUND_RATE_PER_MIN`, `INBOUND_BURST`). `/api/v1/health`, `/api/v1/version`, `/api/v1/attribution`, the docs, and the static UI pages are exempt: the platform polls `/health`, and a 429 there reads as "unhealthy".

**Outbound** — the service throttles its *own* upstream usage to stay inside what each vendor permits. These are the binding constraints on the whole service:

| Upstream | Published limit | On breach |
| :--- | :--- | :--- |
| Open Food Facts — product reads | **15/minute per IP** | *"we reserve the right to deny you access… through IP address ban"* |
| Open Food Facts — search | **10/minute per IP** | as above; also `503` when their *global* limit is exceeded |
| USDA FDC | 3600/hour (`x-ratelimit-limit`) | key throttled |

Search is limited more strictly than product reads, so it has its own budget rather than sharing one and quietly overrunning the tighter of the two.

Budgets are spent by the upstream **clients**, not by one of their callers, so every path is covered by construction: the canonical lookup, the direct `/api/v1/off/*` and `/api/v1/usda/*` endpoints, and the health probes. Guarding only the lookup left the direct endpoints free to overrun — a caller inbound-limited to 60/min could have driven 60 searches/minute at Open Food Facts, six times their limit.

A call refused for budget returns **429** with `Retry-After` on the direct endpoints (it is our throttle, not an upstream fault, so it is not a 502) and **degrades to a partial 200** on the canonical lookup. It is never recorded as a circuit-breaker failure: our own busy minute says nothing about the upstream's health.

One *uncached* lookup spends one call at each. When a budget is exhausted the service **degrades** — that source is skipped and the response comes back partial, with the source absent from `data_sources` — rather than overrunning the limit and getting blocked. This is deliberately not treated as an upstream failure, so it never trips the circuit breaker.

The in-memory cache and the local bulk mirrors are what make a 15/minute budget workable: repeat scans of the same barcode, and any barcode the mirror holds, cost nothing.

## 6. Input Validation Limits

- Barcode paths: numeric GTIN-8/12/13/14 only (`^(\d{8}|\d{12,14})$`); anything else is a `422`.
- Every free-text `q` query parameter (search, USDA/OFF passthroughs, GPC search) is capped at **200 characters** — no real product name approaches this; longer is padding, not intent.
- `limit` parameters carry explicit `ge`/`le` bounds (search: 1–50; data browser rows: max 200).
