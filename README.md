# Nutrition API

A FastAPI service providing unified food intelligence data: one canonical lookup endpoint that merges USDA FoodData Central, Open Food Facts, and the GS1 Global Product Classification (GPC) taxonomy, plus a full GPC browser API.

See [ARCH.md](ARCH.md) for the system architecture and [SPECS.md](SPECS.md) for the API contract and reconciliation rules.

## Architecture

The API aggregates food product data from multiple sources into a single canonical interface:

- **GS1 GPC** — Product taxonomy (Segment → Family → Class → Brick → Attributes), served from a local SQLite cache
- **USDA FoodData Central** — Lab-quality nutrient data (authoritative for nutrition)
- **Open Food Facts** — Crowdsourced product metadata (images, ingredients, allergens, labels)

A `DataOrchestrator` queries USDA and Open Food Facts concurrently (`asyncio.gather`), then layers the results: OFF provides the base product profile, USDA overrides nutrition, and GS1 GPC supplies the category hierarchy. Reconciled responses are returned as a `CanonicalProduct` with per-100g nutrient baselines, a `data_sources` provenance list, and per-source latency telemetry.

**Robustness (Phase 2):**
- Every upstream call is capped at a 2.0s timeout
- Per-source circuit breakers skip a failing upstream for a 60s cooldown after 5 consecutive failures
- Hot GTIN lookups are served from an in-memory TTL cache (default 1024 entries / 300s)
- Upstream failures degrade to a partial `200 OK` — never a `500`

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

API docs at http://localhost:8000/docs (Swagger UI) or http://localhost:8000/redoc

### Docker

```bash
docker build -t nutrition-api .
docker run -p 8080:8080 --env-file .env nutrition-api
```

The image bakes the GPC SQLite database in at build time, so the container starts with no network dependency. `FDC_API_KEY` (see `.env.example`) enables the USDA source.

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
| GET | `/api/gpc/segments/` | List segments (paginated, searchable) |
| GET | `/api/gpc/segments/{code}` | Segment detail with families |
| GET | `/api/gpc/families/` | List families (filterable by segment) |
| GET | `/api/gpc/families/{code}` | Family detail with classes |
| GET | `/api/gpc/classes/` | List classes (filterable by family) |
| GET | `/api/gpc/classes/{code}` | Class detail with bricks |
| GET | `/api/gpc/bricks/` | List bricks (filterable by class) |
| GET | `/api/gpc/bricks/{code}` | Brick detail with attributes |
| GET | `/api/gpc/search/?q=...` | Cross-entity search |

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
