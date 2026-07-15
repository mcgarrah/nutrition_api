# Nutrition API

A FastAPI service providing unified food intelligence data: one canonical lookup endpoint that merges USDA FoodData Central, Open Food Facts, and the GS1 Global Product Classification (GPC) taxonomy, plus a full GPC browser API.

See [ARCH.md](ARCH.md) for the system architecture and [SPECS.md](SPECS.md) for the API contract and reconciliation rules.

## Architecture

The API aggregates food product data from multiple sources into a single canonical interface:

- **GS1 GPC** — Product taxonomy (Segment → Family → Class → Brick → Attributes), served from a local SQLite cache
- **USDA FoodData Central** — Lab-quality nutrient data (authoritative for nutrition), served from a local copy of the bulk dataset with the live API as fallback
- **Open Food Facts** — Crowdsourced product metadata (images, ingredients, allergens, labels)

A `DataOrchestrator` queries USDA and Open Food Facts concurrently (`asyncio.gather`), then layers the results: OFF provides the base product profile, USDA overrides nutrition, and GS1 GPC supplies the category hierarchy. Reconciled responses are returned as a `CanonicalProduct` with per-100g nutrient baselines, a `data_sources` provenance list, and per-source latency telemetry.

**Robustness (Phase 2):**
- Every upstream call is capped at a 2.0s timeout
- Per-source circuit breakers skip a failing upstream for a 60s cooldown after 5 consecutive failures
- Hot GTIN lookups are served from an in-memory TTL cache (default 1024 entries / 300s)
- Upstream failures degrade to a partial `200 OK` — never a `500`

### Local USDA FDC copy

USDA publishes its entire branded corpus twice a year (April and October) as a bulk download. The API imports it locally, so the most common request it serves — "what is this barcode?" — is a disk read rather than a network call:

| | live API | local copy |
|---|---|---|
| barcode lookup | 200–2000 ms | **~25 µs** |
| API key | required | not needed |
| rate limit | 3,600/hour | none |
| lookup method | fuzzy full-text search (FDC has no barcode endpoint) | exact index on GTIN-14 |

The copy holds **442,095 distinct barcodes**. A miss is not a failure — it means the product is newer than the dataset — so the request falls through to the live API, which remains the authority for anything the copy hasn't got. Set `FDC_LOCAL_ENABLED=0` to force every lookup upstream.

```bash
# What's installed, and what USDA currently offers
python scripts/build_fdc_db.py --check

# Refresh (fetches the published 28 MB archive if there is one,
# otherwise rebuilds from USDA's 428 MB bulk zip)
python scripts/build_fdc_db.py --auto-update

# Build from scratch and write the archive
python scripts/build_fdc_db.py
```

The build streams USDA's CSVs straight out of the zip — the 3.1 GB of uncompressed data is never extracted, so peak memory stays around 250 MB. It takes about 5 minutes and produces a 322 MB database plus a 28 MB `.xz`.

The 322 MB database is too large for git and the archive would add ~28 MB to every clone at each refresh, so **neither is committed**: the archive is published as a GitHub release asset (`fdc-YYYY-MM-DD`) and expanded on first startup.

**A barcode is not unique in FDC.** It republishes a product as a new `fdc_id` whenever the label changes, so 2.0M records are really 442,095 barcodes — 4.5 revisions each on average, up to 38 — and 31% of colliding barcodes disagree on calories. The newest revision defines the product; where it is merely *silent* about a nutrient, an earlier revision fills the gap (which recovers 122,205 values).

### Local Open Food Facts copy

Open Food Facts is the *base layer* of every lookup (name, brand, image, ingredients, provisional nutrition) and the slow, rate-limited half of a response. It publishes the whole corpus once a day as a single gzipped CSV (~1.3 GB), and the API imports the usable subset:

| | live API | local copy |
|---|---|---|
| product lookup | 200–500 ms | **~25 µs** |
| rate limit | 15 reads/min per IP (ban on overrun) | none |

The copy holds **~2.24M products** — the roughly half of OFF's 4.5M rows that have a barcode, a name, and at least one nutrient we publish. A miss falls through to the live API, still authoritative for products newer than the export or too sparse to import. Set `OFF_LOCAL_ENABLED=0` to force every lookup upstream.

```bash
python scripts/build_off_db.py --check         # installed vs. available
python scripts/build_off_db.py --auto-update   # fetch the archive, or rebuild
python scripts/build_off_db.py                 # build from scratch
```

The build streams the CSV straight out of the gzip (the 9 GB of decompressed text is never written), takes ~7 minutes, and produces a ~1 GB database plus a ~142 MB `.xz`. As with FDC, neither is committed — the archive is a release asset (`off-YYYY-MM-DD`), expanded on first startup. Nutrient values are stored *raw* (OFF's grams) so `from_off` converts them once, at lookup, exactly as on the live path.

Unlike FDC's twice-yearly release, **OFF rebuilds daily**, so the export has no dated filename; the dataset is identified by its `Last-Modified` date and `--auto-update` compares that. Downloads are named for that timestamp (`off-products-2026-07-14T112659Z.csv.gz`), stamped with the export's own mtime, and **kept rather than overwritten** — so several days can be held side by side and compared. The exact source timestamp is recorded in the database (`source_modified`) and reported by `/health`.

The GPC data is stored in SQLite with a corrected schema that uses junction tables to preserve the many-to-many relationships between bricks and attribute types (the same attribute type can appear on many bricks in the GS1 specification).

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Import GPC data (food segments only)
python scripts/import_gpc_xml.py

# Start the API
uvicorn app.main:app --reload
```

Then open:

- `/` — browser-based lookup tester (product card, per-source contributions, upstream latency, cache round-trip)
- `/docs` — Swagger UI, or `/redoc` for reference-style docs

### Docker

```bash
docker build -t nutrition-api .
docker run -p 8080:8080 --env-file .env nutrition-api
```

The image bakes the GPC SQLite database in at build time, so the container starts with no network dependency. `FDC_API_KEY` (see `.env.example`) enables the USDA source.

### systemd (bare metal / VM / LXC)

To run the API as a service that starts on boot, see [`deploy/`](deploy/):

```bash
sudo install -m 644 deploy/nutrition-api.service /etc/systemd/system/
sudo systemctl enable --now nutrition-api.service
```

### Cached upstream responses

Every Open Food Facts and USDA response is written to `data/responses/` as an individual JSON record — the payload as it arrived, plus a UTC timestamp — and served from there on a repeat lookup.

This is not only an optimisation. Open Food Facts allows **15 requests per minute per IP** and enforces it with an IP ban, and the in-memory cache dies with the process — so before this, *every deploy re-spent that entire allowance* re-fetching barcodes already seen. A stored response costs no request and no rate-limit budget.

```bash
# Turn the corpus into a queryable database
python scripts/import_store_to_sqlite.py

sqlite3 data/responses.sqlite3 \
  "SELECT description, amount, unit FROM usda_foods f
     JOIN usda_nutrients n USING (fdc_id) WHERE n.nutrient_id = 1008;"
```

| Setting | Default | |
| :--- | :--- | :--- |
| `RESPONSE_STORE_DIR` | `data/responses` | where records are written |
| `RESPONSE_STORE_TTL_DAYS` | `30` | how long a record is served before re-fetching |
| `RESPONSE_STORE_ENABLED` | `1` | set `0` to disable |

Timestamps are UTC ISO-8601 with an explicit offset, so they sort correctly as text in SQLite. Nutrients are keyed by **id**, never by name — FDC publishes energy twice under the name "Energy" (kcal and kJ), and a name key silently keeps whichever came last.

### Tests & Linting

```bash
pip install -r requirements-dev.txt
pytest          # unit tests in app/tests/ — no network required
flake8 app/ scripts/
```

CI runs both plus a Docker build on every push and pull request (`.github/workflows/ci.yml`). `.do/app.yaml` deploys the container to DigitalOcean App Platform on push to `main`.

## GPC Data Import

The import script uses the [gs1_gpc](https://github.com/mcgarrah/gs1_gpc_python) library to fetch the latest GPC XML from GS1, falling back to the local cached XML file.

```bash
# Use local cached XML (default)
python scripts/import_gpc_xml.py

# Download latest from GS1
python scripts/import_gpc_xml.py --download

# Use a specific XML file
python scripts/import_gpc_xml.py --xml path/to/file.xml
```

Only the Food/Beverage segment (50000000) is imported. The full GPC taxonomy covers 44 segments including non-food categories (Arts/Crafts, Vehicles, etc.) which are not relevant to a nutrition API.

## API Endpoints

### Unified Lookup

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/lookup/{gtin}` | Canonical product profile merged from all sources (GTIN-8/12/13/14) |

Example: `GET /api/v1/lookup/04963406021372` returns product name, brand, per-100g nutrition, category hierarchy, image, ingredients, `data_sources`, and `upstream_latency_ms`. See [SPECS.md](SPECS.md) for the full contract.

### Source-Specific

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/usda/search?q=...` | Search USDA FDC by keyword |
| GET | `/api/v1/usda/food/{fdc_id}` | USDA food detail by FDC ID |
| GET | `/api/v1/usda/lookup/{upc}` | USDA branded food by UPC/GTIN |
| GET | `/api/v1/off/product/{barcode}` | Open Food Facts product by barcode |
| GET | `/api/v1/off/search?q=...` | Search Open Food Facts by keyword |

### GPC Browser

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/gpc/segments/` | List segments (paginated, searchable) |
| GET | `/api/v1/gpc/segments/{code}` | Segment detail with families |
| GET | `/api/v1/gpc/families/` | List families (filterable by segment) |
| GET | `/api/v1/gpc/families/{code}` | Family detail with classes |
| GET | `/api/v1/gpc/classes/` | List classes (filterable by family) |
| GET | `/api/v1/gpc/classes/{code}` | Class detail with bricks |
| GET | `/api/v1/gpc/bricks/` | List bricks (filterable by class) |
| GET | `/api/v1/gpc/bricks/{code}` | Brick detail with attributes |
| GET | `/api/v1/gpc/search/?q=...` | Cross-entity search over segments, families, classes, bricks **and attributes** (capped; `limit` up to 200, `counts` reports the real totals) |

GPC keeps the specifics in attributes, not brick names — "olive oil" is the attribute value `OLIVE OIL` of *Type of Edible Vegetable or Plant Oil* on the generic *Oils Edible* brick, not a brick of its own. Search therefore reaches into attribute types and values, and each attribute match carries the bricks that hold it, so `?q=olive` finds the oils brick that a description-only search would miss. Filter with `category=attributes`.

### Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check with GPC segment count |
| GET | `/api/v1/version` | API version and git hash |

## Project Structure

```
nutrition_api/
├── app/
│   ├── main.py              # FastAPI application entry point
│   ├── database.py          # Async SQLite connection management
│   ├── gpc/
│   │   ├── models.py        # Pydantic models for GPC hierarchy
│   │   └── routes.py        # GPC API endpoints
│   ├── core/
│   │   ├── models.py        # CanonicalProduct / NutrientValue models
│   │   ├── orchestrator.py  # Layered merge + TTL lookup cache
│   │   ├── resilience.py    # Circuit breakers + upstream timeouts
│   │   ├── usda_fdc.py      # USDA FDC async wrapper
│   │   ├── open_food_facts.py  # OFF async wrapper
│   │   └── *_routes.py      # Lookup, USDA, and OFF endpoints
│   └── tests/               # Pytest suite (upstreams mocked)
├── data/
│   ├── imports/en-v20251127.xml  # Cached GPC XML source
│   └── gpc.sqlite3               # Generated database (gitignored)
├── scripts/
│   └── import_gpc_xml.py    # XML-to-SQLite importer
├── Dockerfile               # Multi-stage build, GPC DB baked in
├── .do/app.yaml             # DigitalOcean App Platform spec
├── .github/workflows/ci.yml # flake8 + pytest + docker build
└── requirements.txt
```

## Data Model

The SQLite schema uses junction tables to correctly model the GPC hierarchy:

```
segments ──1:N──> families ──1:N──> classes ──1:N──> bricks
                                                       │
                                          brick_attribute_types (M:N junction)
                                                       │
                                                 attribute_types
                                                       │
                                          attribute_type_values (M:N junction)
                                                       │
                                                 attribute_values
```

This differs from the original Django implementation (in both `food_service_nutrition` and `shiny-shop`) which used single foreign keys and lost data when the same attribute type appeared on multiple bricks.

## Prior Art

This project extracts and improves the GPC API from:
- [shiny-shop](https://github.com/mcgarrah/shiny-shop) — Django app with DRF-based GPC API (deployed at nutrition.mcgarrah.org)
- [gs1_gpc_python](https://github.com/mcgarrah/gs1_gpc_python) — GPC XML downloader and parser library
- [food_service_nutrition](https://github.com/mcgarrah/food_service_nutrition) — Earlier Django prototype with GPC models

## License

MIT — Copyright (c) 2026 Michael McGarrah
