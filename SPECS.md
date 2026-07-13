# SPECS.md - Technical Specifications

## 1. OpenAPI & API Contracts

### Endpoint: `GET /api/v1/lookup/{gtin}`
Retrieves a standardized, consolidated profile of a food product using its GTIN/UPC barcode string.

#### Path Parameters
- `gtin` (string, required): A numeric string matching standard barcode specifications (GTIN-8, GTIN-12, GTIN-13, or GTIN-14). Malformed values are rejected with `422 Unprocessable Entity`.

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
  "calories_kcal": 42.0,
  "protein": { "value": 0.0, "unit": "g" },
  "fat": { "value": 0.0, "unit": "g" },
  "carbohydrates": { "value": 10.6, "unit": "g" },
  "fiber": null,
  "sugars": { "value": 10.6, "unit": "g" },
  "sodium": { "value": 4.0, "unit": "mg" },
  "image_url": "https://static.openfoodfacts.org/images/products/049/634/060/21372/front_en.400.jpg",
  "ingredients_text": "Carbonated water, high fructose corn syrup, caramel color, phosphoric acid, natural flavors, caffeine",
  "allergens": [],
  "labels": [],
  "data_sources": ["USDA_FDC", "OpenFoodFacts", "GS1_GPC"],
  "upstream_latency_ms": { "USDA_FDC": 110.5, "OpenFoodFacts": 95.2, "GS1_GPC": 5.1 }
}
```

#### Error Responses
- `404 Not Found` — no upstream source has any data for the GTIN.
- `422 Unprocessable Entity` — the GTIN is not a valid numeric barcode string.
- `429 Too Many Requests` — the caller exceeded the inbound rate limit. Carries a `Retry-After` header (seconds).

## 4. Rate Limits

**Inbound** — 60 requests/minute per client IP, burst 20 (`INBOUND_RATE_PER_MIN`, `INBOUND_BURST`). `/api/v1/health`, `/api/v1/version`, the docs, and the UI are exempt: the platform polls `/health`, and a 429 there reads as "unhealthy".

**Outbound** — the service throttles its *own* upstream usage to stay inside what each vendor permits. These are the binding constraints on the whole service:

| Upstream | Published limit | On breach |
| :--- | :--- | :--- |
| Open Food Facts | **15 product reads/minute per IP** | *"we reserve the right to deny you access… through IP address ban"* |
| USDA FDC | 3600/hour (`x-ratelimit-limit`) | key throttled |

One *uncached* lookup spends one call at each. When a budget is exhausted the service **degrades** — that source is skipped and the response comes back partial, with the source absent from `data_sources` — rather than overrunning the limit and getting blocked. This is deliberately not treated as an upstream failure, so it never trips the circuit breaker.

The in-memory cache is what makes a 15/minute budget workable: repeat scans of the same barcode cost nothing.

Upstream failures never surface as `5xx`: the response degrades to a partial `200 OK` with the failed source absent from `data_sources`.

## 2. Pydantic Verification Schemas
Nutritional units are systematically mapped to a standardized baseline measurement of **100g** or **100mL** of product to eliminate arbitrary manufacturer portion definitions and ensure mathematical parity inside data science operations.

```python
class NutrientValue(BaseModel):
    value: float
    unit: str = "g"

class CanonicalProduct(BaseModel):
    gtin: str
    product_name: str = "Unknown"
    brand: str | None = None
    category_hierarchy: list[str] = []
    calories_kcal: float | None = None
    protein: NutrientValue | None = None
    fat: NutrientValue | None = None
    carbohydrates: NutrientValue | None = None
    fiber: NutrientValue | None = None
    sugars: NutrientValue | None = None
    sodium: NutrientValue | None = None
    image_url: str | None = None
    ingredients_text: str | None = None
    allergens: list[str] = []
    labels: list[str] = []
    data_sources: list[str] = []
    upstream_latency_ms: dict[str, float] = {}
```

The authoritative definition lives in `app/core/models.py`.

## 3. Reconciliation Matrices & Ranked Truth Rules
When fields are provided concurrently across vendors, conflicts are evaluated deterministically using an embedded ingestion table:

| Objective Field | Authoritative Source | Validation Strategy / Transformation Rules |
| :--- | :--- | :--- |
| **Nutrients** | **USDA FDC** | Reconciles raw laboratory outputs; per-100g baselines. Open Food Facts values are provisional and overridden when USDA data exists. |
| **Media / Images** | **Open Food Facts** | Validates public URLs; grabs real-time label photography strings. |
| **Taxonomy** | **GS1 GPC** | Maps classification trees down to the specific Brick segments via local SQLite cache; Open Food Facts category tags are the fallback. |
| **Ingredients** | **Open Food Facts** | Parses text strings from public product labeling array; USDA ingredients used when OFF has none. |

## 4. Resilience Contracts
- Every upstream call is capped at **2.0 seconds** (`UPSTREAM_TIMEOUT_S`).
- Per-source circuit breakers open after **5 consecutive failures** and skip the source for a **60s cooldown** before half-open probing.
- Successful lookups are cached in-memory for **300s** (`LOOKUP_CACHE_TTL_S`), max **1024 entries** (`LOOKUP_CACHE_MAX_SIZE`).
