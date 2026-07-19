"""
Open Food Facts products, served from a local copy of the daily export.

Open Food Facts is the base layer of every lookup — name, brand, image,
ingredients, provisional nutrition — and it is the slow, rate-limited half of a
response. A live product read is a few hundred milliseconds over the network and
costs one of OFF's 15-per-minute-per-IP tokens, whose stated penalty for
overrun is an IP ban. A local read is a few microseconds and costs nothing.

The copy holds the usable subset of the ~4.5M-row export — a product needs a
barcode, a name, and at least one nutrient we publish. A miss is not a failure:
it falls through to the live API, which stays authoritative for products newer
than the export, or too sparse to have been imported.

The record is shaped exactly like the live wrapper's _format_product output,
nutrients and all, and the nutrient values are stored *raw* (OFF's grams) so
that app.core.nutrients.from_off — the same conversion the live path runs — is
applied once, at lookup, in one place.

The database is distributed as an xz archive (~142 MB, a seventh of the 1 GB
database) and expanded on first use. Set OFF_LOCAL_ENABLED=0 to force every
lookup through the live API.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import logging
import lzma
import os
import re
import sqlite3
import time
from pathlib import Path

import aiosqlite

from .nutrients import NUTRIENTS
from .usda_fdc import normalize_gtin

_DATASET_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = Path(os.environ.get("OFF_DB_PATH", DATA_DIR / "off.sqlite3"))
ARCHIVE_PATH = Path(os.environ.get("OFF_ARCHIVE_PATH", DATA_DIR / "off.sqlite3.xz"))

ENABLED = os.environ.get("OFF_LOCAL_ENABLED", "1") not in ("0", "false", "False")

_db: aiosqlite.Connection | None = None
_metadata: dict[str, str] = {}

_TEXT_COLUMNS = (
    "product_name", "brands", "image_url", "ingredients_text", "quantity",
    "serving_size", "categories", "allergens", "labels",
)
# The three that the live SDK returns as lists, stored here comma-joined.
_LIST_COLUMNS = ("categories", "allergens", "labels")
_NUTRIENT_FIELDS = tuple(spec.field for spec in NUTRIENTS)


def ensure_database() -> bool:
    """Expand the compressed database if it isn't already on disk.

    Called once at startup so the cost is visible, not buried in a caller's
    first lookup. Returns True if a usable database is present.
    """
    if not ENABLED:
        return False
    if DB_PATH.exists():
        return True
    if not ARCHIVE_PATH.exists():
        logger.info(
            "No local OFF database and no archive at %s; product lookups will "
            "go to the OFF API. Build one with scripts/build_off_db.py.",
            ARCHIVE_PATH,
        )
        return False

    started = time.monotonic()
    logger.info("Expanding %s (%.0f MB)...", ARCHIVE_PATH.name,
                ARCHIVE_PATH.stat().st_size / 1e6)
    partial = DB_PATH.with_suffix(".expanding")
    try:
        with lzma.open(ARCHIVE_PATH, "rb") as src, open(partial, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        os.replace(partial, DB_PATH)
    except (OSError, lzma.LZMAError) as e:
        logger.error("Could not expand %s: %s", ARCHIVE_PATH.name, e)
        partial.unlink(missing_ok=True)
        return False

    logger.info("Expanded to %s (%.0f MB) in %.1fs", DB_PATH.name,
                DB_PATH.stat().st_size / 1e6, time.monotonic() - started)
    return True


async def _connect() -> aiosqlite.Connection | None:
    global _db, _metadata
    if _db is not None:
        return _db
    if not ENABLED or not DB_PATH.exists():
        return None
    # Read-only: nothing in the request path may write here, and opening
    # read-only turns a stray write into a loud error rather than a corrupted
    # copy we would then serve.
    _db = await aiosqlite.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    _db.row_factory = aiosqlite.Row
    try:
        rows = await _db.execute_fetchall("SELECT key, value FROM off_metadata")
        _metadata = {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error("Local OFF database at %s is unusable: %s", DB_PATH, e)
        await _db.close()
        _db = None
        return None
    logger.info("Local OFF database ready: %s products from %s",
                _metadata.get("products", "?"), _metadata.get("dataset", "?"))
    return _db


def is_available() -> bool:
    return ENABLED and DB_PATH.exists()


def _split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag for tag in (part.strip() for part in value.split(",")) if tag]


async def get_by_gtin(gtin: str) -> dict | None:
    """Look up a product by barcode. Returns a _format_product-shaped record.

    Nutrients come back raw (OFF's grams) under `nutrients_per_100g`, keyed by
    our field names — exactly what the live wrapper hands the orchestrator, so
    from_off does the unit conversion once, at the same point in the pipeline.
    """
    db = await _connect()
    if db is None:
        return None

    target = normalize_gtin(gtin)
    if not target:
        return None

    columns = ", ".join((*_TEXT_COLUMNS, *_NUTRIENT_FIELDS))
    async with db.execute(
        f"SELECT gtin14, {columns} FROM products WHERE gtin14 = ?", (target,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    nutrients_per_100g = {
        field: row[field] for field in _NUTRIENT_FIELDS if row[field] is not None
    }
    return {
        "barcode": row["gtin14"],
        "product_name": row["product_name"],
        "brands": row["brands"],
        "image_url": row["image_url"],
        "ingredients_text": row["ingredients_text"],
        "quantity": row["quantity"],
        "serving_size": row["serving_size"],
        "categories": _split_tags(row["categories"]),
        "allergens": _split_tags(row["allergens"]),
        "labels": _split_tags(row["labels"]),
        "nutrients_per_100g": nutrients_per_100g,
    }


def _read_metadata() -> dict[str, str]:
    """Read metadata synchronously, for /health before any lookup has connected."""
    global _metadata
    if _metadata:
        return _metadata
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT key, value FROM off_metadata").fetchall()
        finally:
            db.close()
        _metadata = {key: value for key, value in rows}
    except sqlite3.Error as e:
        logger.warning("Could not read metadata from %s: %s", DB_PATH, e)
        return {}
    return _metadata


def provenance() -> dict:
    """Origin tag for a local hit: the dataset and its date, for the response."""
    metadata = _read_metadata()
    dataset = metadata.get("dataset")
    match = _DATASET_DATE.search(dataset or "")
    return {
        "origin": "local",
        "dataset": dataset,
        "dataset_date": match.group(0) if match else None,
    }


def stats() -> dict:
    """What the /health endpoint reports about the local copy."""
    if not ENABLED:
        return {"status": "disabled"}
    if not DB_PATH.exists():
        return {"status": "absent", "detail": "no local database; using the OFF API"}
    metadata = _read_metadata()
    if not metadata:
        return {"status": "error", "detail": "database present but unreadable"}

    def _int(key):
        return int(metadata[key]) if key in metadata else None

    return {
        "status": "ok",
        "dataset": metadata.get("dataset"),
        "products": _int("products"),
        # PLAN.md item 12: absent (None) for a mirror built before this was
        # added, not an error -- an older database simply has nothing to
        # report here yet.
        "rows_read": _int("rows_read"),
        "excluded": _int("excluded"),
        "deduped": _int("deduped"),
        "source_modified": metadata.get("source_modified"),
        "imported_at": metadata.get("import_timestamp"),
        "size_mb": round(DB_PATH.stat().st_size / 1e6, 1),
    }


async def close() -> None:
    """Close the connection; aiosqlite's non-daemon thread hangs exit otherwise."""
    global _db, _metadata
    if _db is not None:
        await _db.close()
        _db = None
    _metadata = {}
