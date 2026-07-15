"""
USDA FDC branded foods, served from a local copy of the bulk dataset.

FDC publishes its entire branded corpus twice a year, and importing it locally
turns the most common request this API serves — "what is this barcode?" — from a
network round trip into a disk read. Measured on the April 2026 corpus: 442,095
barcodes, resolved in ~23 µs against ~200-2000 ms upstream.

That is not merely faster. Everything that makes the FDC path fragile lives in
the network call, and a local hit skips all of it:

  * no API key, and no 3,600/hour ceiling to budget against
  * no fuzzy full-text search — FDC has no barcode endpoint, so the live path
    searches for the digits and hopes, which is how a lookup for "00000000" once
    returned chicken nuggets
  * no two-round-trip timeout, no circuit breaker, no thread pool

The database is distributed as an xz archive (~28 MB, about a tenth of the
321 MB database) because the database itself is too large for git. It is
expanded on first use and thereafter read directly.

The API is not retired. It remains the source for anything the local copy does
not have — products released since the last dataset, and anything a caller wants
straight from upstream. Refresh the copy with `scripts/build_fdc_db.py --auto-update`.

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

from . import nutrients as nutrient_spec
from .usda_fdc import normalize_gtin

_DATASET_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = Path(os.environ.get("FDC_DB_PATH", DATA_DIR / "fdc.sqlite3"))
ARCHIVE_PATH = Path(os.environ.get("FDC_ARCHIVE_PATH", DATA_DIR / "fdc.sqlite3.xz"))

# Set FDC_LOCAL_ENABLED=0 to force every lookup through the live API.
ENABLED = os.environ.get("FDC_LOCAL_ENABLED", "1") not in ("0", "false", "False")

_db: aiosqlite.Connection | None = None
_metadata: dict[str, str] = {}

# The product columns, in the order the row is read.
_COLUMNS = (
    "fdc_id", "published", "description", "brand_owner", "brand_name",
    "ingredients", "serving_size", "serving_size_unit", "household_serving",
    "category",
)


def ensure_database() -> bool:
    """Expand the compressed database if it isn't already on disk.

    Returns True if a usable database is present. Called once at startup rather
    than lazily on the first request, so the cost lands somewhere visible instead
    of inside a caller's lookup.
    """
    if not ENABLED:
        return False
    if DB_PATH.exists():
        return True
    if not ARCHIVE_PATH.exists():
        logger.info(
            "No local FDC database and no archive at %s; barcode lookups will "
            "go to the FDC API. Build one with scripts/build_fdc_db.py.",
            ARCHIVE_PATH,
        )
        return False

    started = time.monotonic()
    logger.info("Expanding %s (%.0f MB)...", ARCHIVE_PATH.name,
                ARCHIVE_PATH.stat().st_size / 1e6)
    # Expand beside the target and rename into place: a reader must never find a
    # half-written database, and an interrupted startup must not leave one.
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
    # Read-only: nothing in the request path may ever write to this file, and
    # opening it read-only means a stray UPDATE fails loudly rather than
    # corrupting the copy we would then serve for six months.
    _db = await aiosqlite.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    _db.row_factory = aiosqlite.Row
    try:
        rows = await _db.execute_fetchall("SELECT key, value FROM fdc_metadata")
        _metadata = {r[0]: r[1] for r in rows}
    except Exception as e:  # a database we cannot read is one we will not serve
        logger.error("Local FDC database at %s is unusable: %s", DB_PATH, e)
        await _db.close()
        _db = None
        return None
    logger.info("Local FDC database ready: %s barcodes from %s",
                _metadata.get("barcodes", "?"), _metadata.get("dataset", "?"))
    return _db


def is_available() -> bool:
    return ENABLED and DB_PATH.exists()


async def get_by_gtin(gtin: str) -> dict | None:
    """Look up a branded food by barcode. Returns an FDC-shaped record, or None.

    The record is shaped exactly like the one usda_fdc.search_by_upc returns —
    including a `nutrients` list keyed by FDC id — so the orchestrator cannot
    tell the two apart, and neither tier gets its own copy of the mapping rules.
    """
    db = await _connect()
    if db is None:
        return None

    target = normalize_gtin(gtin)
    if not target:
        return None

    columns = ", ".join(_COLUMNS)
    fields = ", ".join(spec.field for spec in nutrient_spec.NUTRIENTS)
    async with db.execute(
        f"SELECT {columns}, {fields} FROM foods WHERE gtin14 = ?", (target,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    values = {spec.field: row[spec.field] for spec in nutrient_spec.NUTRIENTS}
    record = {name: row[name] for name in _COLUMNS}
    record["data_type"] = "Branded"
    record["nutrients"] = nutrient_spec.to_usda_entries(values)
    return record


def _read_metadata() -> dict[str, str]:
    """Read the database's metadata without going through the async connection.

    /health can be polled before any lookup has opened one, and reporting the
    copy as present but nameless — "dataset: null" — tells an operator nothing
    about how stale it is, which is the one thing they need to know.
    """
    global _metadata
    if _metadata:
        return _metadata
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT key, value FROM fdc_metadata").fetchall()
        finally:
            db.close()
        _metadata = {key: value for key, value in rows}
    except sqlite3.Error as e:
        logger.warning("Could not read metadata from %s: %s", DB_PATH, e)
        return {}
    return _metadata


def provenance() -> dict:
    """Origin tag for a local hit: the dataset and its date, for the response.

    Cheap — the metadata is read once and cached, so this costs nothing per
    lookup after the first.
    """
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
        return {"status": "absent", "detail": "no local database; using the FDC API"}
    metadata = _read_metadata()
    if not metadata:
        return {"status": "error", "detail": "database present but unreadable"}
    return {
        "status": "ok",
        "dataset": metadata.get("dataset"),
        "barcodes": int(metadata["barcodes"]) if "barcodes" in metadata else None,
        "source_modified": metadata.get("source_modified"),
        "imported_at": metadata.get("import_timestamp"),
        "size_mb": round(DB_PATH.stat().st_size / 1e6, 1),
    }


async def close() -> None:
    """Close the connection. aiosqlite runs a non-daemon thread per connection,
    so an unclosed one keeps the interpreter alive at exit."""
    global _db, _metadata
    if _db is not None:
        await _db.close()
        _db = None
    _metadata = {}
