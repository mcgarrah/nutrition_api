"""
Nutrition API — FastAPI application.

Unified food intelligence API aggregating USDA FDC, Open Food Facts,
and GS1 Global Product Classification data.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
Repository: https://github.com/mcgarrah/nutrition_api
"""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from .database import close_db
from .gpc.routes import router as gpc_router
from .core.usda_routes import router as usda_router
from .core.off_routes import router as off_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build or update GPC database on startup
    from .database import DB_PATH
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    import subprocess, sys
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
    yield
    await close_db()


app = FastAPI(
    title="Nutrition API",
    description=(
        "Unified food intelligence API aggregating USDA FoodData Central, "
        "Open Food Facts, and GS1 Global Product Classification data.\n\n"
        "**GPC Browser**: Browse the full GS1 GPC taxonomy hierarchy — "
        "Segments, Families, Classes, Bricks, and Attributes.\n\n"
        "**USDA FDC**: Search and retrieve lab-quality nutritional data.\n\n"
        "**Open Food Facts**: Crowdsourced product data — images, ingredients, labels.\n\n"
        "Author: Michael McGarrah (mcgarrah@gmail.com)\n"
        "Website: https://mcgarrah.org"
    ),
    version="0.1.0",
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    contact={"name": "Michael McGarrah", "url": "https://mcgarrah.org"},
    lifespan=lifespan,
)

app.include_router(gpc_router)
app.include_router(usda_router)
app.include_router(off_router)


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

    usda_status = await usda_fdc.check_connectivity()
    result["usda_fdc"] = usda_status
    if usda_status["status"] == "error":
        result["status"] = "degraded"

    off_status = await off.check_connectivity()
    result["open_food_facts"] = off_status
    if off_status["status"] == "error":
        result["status"] = "degraded"

    return result


@app.get("/api/v1/version", tags=["Operations"], summary="API version")
async def version():
    import os
    return {
        "version": "0.1.0",
        "git_hash": os.environ.get("GIT_HASH", "dev"),
    }
