# Nutrition API

A FastAPI service providing unified food intelligence data, starting with a GS1 Global Product Classification (GPC) browser and API.

## Architecture

The API aggregates food product data from multiple sources into a single canonical interface:

- **GS1 GPC** — Product taxonomy (Segment → Family → Class → Brick → Attributes)
- **USDA FoodData Central** — Lab-quality nutrient data (planned)
- **Open Food Facts** — Crowdsourced product metadata (planned)

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
│   └── core/                # Future: USDA, OFF, canonical models
├── data/
│   ├── gpc_november_2024.xml  # Cached GPC XML source
│   └── gpc.sqlite3           # Generated database (gitignored)
├── scripts/
│   └── import_gpc_xml.py    # XML-to-SQLite importer
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
