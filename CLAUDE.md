# CLAUDE.md - Developer Guide

## Build & Deployment Commands
- **Run Local Dev Server:** `uvicorn app.main:app --reload --port 8080`
- **Build Docker Container:** `docker build -t nutrition-api .`
- **Run Container Locally:** `docker run -p 8080:8080 --env-file .env nutrition-api`
- **Import GPC Data:** `python scripts/import_gpc_xml.py` (see README for options)

## Code & Quality Standards
- **Linting:** Use `flake8` for style enforcement. Run `flake8 app/ scripts/` (config in `.flake8`).
- **Formatting:** Use `black` for standardized Python formatting.
- **Testing:** Run suite via `pytest`. Test files live in `app/tests/`. No network access required — upstream sources are mocked.
- **Imports:** Group standard library imports first, then third-party libraries (FastAPI, Pydantic), followed by internal application modules.

## Architecture Guidelines
- Always write asynchronous route handlers (`async def`) for external I/O tasks.
- Enforce strict typing via Pydantic v2. The unified lookup response derives from `CanonicalProduct` (`app/core/models.py`).
- Use a layered mapping strategy (`app/core/orchestrator.py`): Open Food Facts parses basic labels/images, USDA FDC overrides nutrient values, and the GS1 SQLite cache handles taxonomic mapping.
- Graceful degradation: external network calls go through the per-source circuit breakers in `app/core/resilience.py` with short timeouts (max 2.0s). Partial upstream failures must degrade to a partial `200 OK` response, never a 500.
- Hot GTIN lookups are cached in-memory (`cachetools.TTLCache`); only results with at least one contributing source are cached.
