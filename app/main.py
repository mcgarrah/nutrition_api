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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build or update GPC database on startup
    from .database import DB_PATH
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    import subprocess, sys
    if not DB_PATH.exists():
        # No database — build from whatever is available
        subprocess.run(
            [sys.executable, str(scripts_dir / "import_gpc_xml.py")],
            check=True,
        )
    else:
        # Database exists — check if GS1 has a newer version
        subprocess.run(
            [sys.executable, str(scripts_dir / "import_gpc_xml.py"), "--auto-update"],
            check=True,
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
        "Author: Michael McGarrah (mcgarrah@gmail.com)\n"
        "Website: https://mcgarrah.org"
    ),
    version="0.1.0",
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    contact={"name": "Michael McGarrah", "url": "https://mcgarrah.org"},
    lifespan=lifespan,
)

app.include_router(gpc_router)


@app.get("/api/v1/health", tags=["Operations"], summary="Health check")
async def health():
    from .database import get_db
    try:
        db = await get_db()
        row = await db.execute_fetchall("SELECT COUNT(*) FROM segments")
        # Fetch GPC data freshness metadata
        meta_rows = await db.execute_fetchall("SELECT key, value FROM gpc_metadata")
        metadata = {r[0]: r[1] for r in meta_rows}
        return {
            "status": "ok",
            "gpc_segments": row[0][0],
            "gpc_version": metadata.get("gpc_version"),
            "gpc_xml_date": metadata.get("xml_date"),
            "gpc_import_timestamp": metadata.get("import_timestamp"),
        }
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


@app.get("/api/v1/version", tags=["Operations"], summary="API version")
async def version():
    import os
    return {
        "version": "0.1.0",
        "git_hash": os.environ.get("GIT_HASH", "dev"),
    }
