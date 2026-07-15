"""
Nutrition API — FastAPI application.

Unified food intelligence API aggregating USDA FDC, Open Food Facts,
and GS1 Global Product Classification data.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
Repository: https://github.com/mcgarrah/nutrition_api
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .core import attribution
from .core import fdc_local
from .core import off_local
from .core import ratelimit
from .core import store
from .database import close_db
from .gpc.routes import router as gpc_router
from .core.usda_routes import router as usda_router
from .core.off_routes import router as off_router
from .core.lookup_routes import router as lookup_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build or update GPC database on startup
    from .database import DB_PATH
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    import subprocess
    import sys
    import_script = str(scripts_dir / "import_gpc_xml.py")

    if not DB_PATH.exists():
        # No database — must build from whatever is available
        subprocess.run(
            [sys.executable, import_script],
            check=True,
        )
    else:
        # Database exists — check if GS1 has a newer version.
        # Non-fatal: if the check fails, we continue with existing data.
        try:
            subprocess.run(
                [sys.executable, import_script, "--auto-update"],
                check=True,
                timeout=120,  # hard cap: don't block startup > 2 minutes
            )
        except subprocess.TimeoutExpired:
            import logging
            logging.warning(
                "GPC auto-update timed out after 120s. Continuing with existing data."
            )
        except subprocess.CalledProcessError as e:
            import logging
            logging.warning(
                "GPC auto-update failed (exit code %d). Continuing with existing data.",
                e.returncode,
            )
    # Expand the compressed FDC copy, if we have one. A miss here is not
    # fatal: barcode lookups fall back to the live FDC API.
    fdc_local.ensure_database()
    off_local.ensure_database()

    yield
    await close_db()
    await fdc_local.close()
    await off_local.close()


app = FastAPI(
    title="Nutrition API",
    description=(
        "Unified food intelligence API aggregating USDA FoodData Central, "
        "Open Food Facts, and GS1 Global Product Classification data.\n\n"
        "**GPC Browser**: Browse the full GS1 GPC taxonomy hierarchy — "
        "Segments, Families, Classes, Bricks, and Attributes.\n\n"
        "**USDA FDC**: Search and retrieve lab-quality nutritional data.\n\n"
        "**Open Food Facts**: Crowdsourced product data — images, ingredients, labels.\n\n"
        "---\n\n"
        "**Attribution.** This API redistributes data from Open Food Facts, whose "
        "database is licensed [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1.0/) "
        "and whose product images are licensed CC BY-SA 3.0 — attribution is required "
        "and derived databases are share-alike. USDA FoodData Central is a U.S. "
        "Government work in the public domain. GS1 publishes the GPC taxonomy for open "
        "use. Per-source terms: `GET /api/v1/attribution`.\n\n"
        "Author: Michael McGarrah (mcgarrah@gmail.com)\n"
        "Website: https://mcgarrah.org"
    ),
    version="0.1.0",
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    contact={"name": "Michael McGarrah", "url": "https://mcgarrah.org"},
    lifespan=lifespan,
)

# Public read-only data API — allow cross-origin GETs from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Paths the limiter must never shed. /health is polled by the platform, and a
# 429 there reads as "unhealthy" — the rate limiter would get the container
# restarted. The docs and UI are static and cost no upstream calls.
_RATE_LIMIT_EXEMPT = ("/api/v1/health", "/api/v1/version", "/api/v1/attribution",
                      "/docs", "/redoc", "/openapi.json", "/ui")


def _client_key(request: Request) -> str:
    """Identify the caller for rate limiting.

    Behind DigitalOcean's router the socket address is the proxy, so the real
    client is the first hop in X-Forwarded-For. Trusting that header is only
    safe because we are always behind a proxy that sets it; exposed directly,
    a caller could spoof it to dodge the limit.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Shed sustained excess from a single caller.

    This is what keeps our *upstream* spend inside Open Food Facts' budget of
    15 requests/minute per IP — a budget they enforce with an IP ban. Without
    it, one enthusiastic client walking distinct barcodes gets the whole
    deployment blocked for everybody.
    """
    if request.url.path.startswith(_RATE_LIMIT_EXEMPT) or request.url.path == "/":
        return await call_next(request)

    key = _client_key(request)
    if not ratelimit.inbound_limiter.try_acquire(key):
        retry_after = ratelimit.inbound_limiter.retry_after(key)
        logger.warning("Rate limit exceeded for %s on %s", key, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Slow down."},
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )

    return await call_next(request)

app.include_router(lookup_router)
app.include_router(gpc_router)
app.include_router(usda_router)
app.include_router(off_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
async def index():
    """Serve the lookup tester UI at the root."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/gpc", include_in_schema=False)
async def gpc_browser():
    """Serve the GPC taxonomy browser UI."""
    return FileResponse(STATIC_DIR / "gpc.html")


@app.get("/api/v1/health", tags=["Operations"], summary="Health check")
async def health():
    from .database import get_db
    from .core import usda_fdc
    from .core import open_food_facts as off
    result = {"status": "ok"}
    try:
        db = await get_db()
        row = await db.execute_fetchall("SELECT COUNT(*) FROM segments")
        meta_rows = await db.execute_fetchall("SELECT key, value FROM gpc_metadata")
        metadata = {r[0]: r[1] for r in meta_rows}
        result["gpc"] = {
            "status": "ok",
            "segments": row[0][0],
            "version": metadata.get("gpc_version"),
            "xml_date": metadata.get("xml_date"),
            "import_timestamp": metadata.get("import_timestamp"),
        }
    except Exception as e:
        result["gpc"] = {"status": "error", "detail": str(e)}
        result["status"] = "degraded"

    # Probe the upstreams concurrently: awaited in series, a health check that
    # is already bounded per-source would still take the *sum* of the timeouts.
    usda_status, off_status = await asyncio.gather(
        usda_fdc.check_connectivity(),
        off.check_connectivity(),
    )

    result["usda_fdc"] = usda_status
    if usda_status["status"] == "error":
        result["status"] = "degraded"

    result["open_food_facts"] = off_status
    if off_status["status"] == "error":
        result["status"] = "degraded"

    # The response store is what keeps repeat lookups off the upstreams, so its
    # state is worth reporting: a store that has quietly stopped writing means
    # every restart goes back to spending Open Food Facts' allowance.
    result["usda_fdc_local"] = fdc_local.stats()
    result["open_food_facts_local"] = off_local.stats()

    result["response_store"] = store.stats()

    return result


@app.get(
    "/api/v1/attribution",
    tags=["Operations"],
    summary="Data sources, licences and attribution",
)
async def attribution_endpoint():
    """Attribution and licence terms for every source this API redistributes.

    Open Food Facts data is published under the Open Database License (ODbL
    1.0), with product images under CC BY-SA 3.0. Both require attribution and
    ODbL adds a share-alike obligation on derived databases — so this is a
    licence condition, not a courtesy.
    """
    return {"sources": attribution.SOURCE_ATTRIBUTION}


@app.get("/api/v1/version", tags=["Operations"], summary="API version")
async def version():
    import os
    return {
        "version": "0.1.0",
        "git_hash": os.environ.get("GIT_HASH", "dev"),
    }
