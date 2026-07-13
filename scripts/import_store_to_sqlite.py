#!/usr/bin/env python3
"""
Import the on-disk response store into SQLite.

The store (`data/responses/`) is one JSON file per upstream response: what Open
Food Facts or USDA actually said, and when. This turns that corpus into a
queryable database — for analysis, for offline work, and so a future session can
start from real payloads rather than re-hitting somebody else's rate-limited API.

The full payload is preserved verbatim in a JSON column, with the fields worth
querying lifted out beside it. Nothing is discarded: a flattened column can be
wrong or go out of date, but the payload it came from cannot.

Timestamps are stored as **UTC ISO-8601 text with an explicit offset**, exactly
as the store records them. ISO-8601 in UTC sorts correctly as text, which SQLite
compares lexically — a local timestamp would sort wrong and mean nothing.

Usage:
    python scripts/import_store_to_sqlite.py
    python scripts/import_store_to_sqlite.py --db data/responses.sqlite3
    python scripts/import_store_to_sqlite.py --store data/responses --verbose

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core import store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

DEFAULT_DB = REPO_ROOT / "data" / "responses.sqlite3"

SCHEMA = """
-- Every table keeps `payload` verbatim. The columns beside it are conveniences
-- lifted out for querying; the payload is the record of what was actually said.
--
-- fetched_at is UTC ISO-8601 with an explicit offset, e.g.
-- '2026-07-13T21:04:05.123456+00:00'. SQLite compares text lexically, and
-- ISO-8601 in UTC sorts correctly under that comparison. A local timestamp
-- would not, and would not say which local.

CREATE TABLE IF NOT EXISTS off_products (
    barcode        TEXT PRIMARY KEY,
    fetched_at     TEXT NOT NULL,
    product_name   TEXT,
    brands         TEXT,
    image_url      TEXT,
    payload        TEXT NOT NULL      -- the raw Open Food Facts response
);

CREATE TABLE IF NOT EXISTS usda_foods (
    fdc_id         INTEGER PRIMARY KEY,
    fetched_at     TEXT NOT NULL,
    description    TEXT,
    brand_owner    TEXT,
    data_type      TEXT,
    payload        TEXT NOT NULL      -- the food record, nutrients and all
);

-- Which FDC food a barcode resolved to. FDC has no barcode endpoint, so this
-- mapping is the expensive part to rediscover: it costs a fuzzy full-text
-- search that has to be verified against gtin_upc.
CREATE TABLE IF NOT EXISTS usda_upc_map (
    gtin14         TEXT PRIMARY KEY,
    fdc_id         INTEGER NOT NULL,
    fetched_at     TEXT NOT NULL
);

-- Nutrients, flattened out of usda_foods so they can be queried directly.
-- Keyed by nutrient id, never by name: FDC publishes energy twice under the
-- identical name "Energy" — kcal (1008) and kJ (1062) — so a name key silently
-- keeps whichever came last, and reports cheddar at 1710 kcal.
CREATE TABLE IF NOT EXISTS usda_nutrients (
    fdc_id         INTEGER NOT NULL REFERENCES usda_foods(fdc_id),
    nutrient_id    INTEGER NOT NULL,
    name           TEXT,
    amount         REAL,
    unit           TEXT,
    PRIMARY KEY (fdc_id, nutrient_id)
);

CREATE INDEX IF NOT EXISTS idx_off_name ON off_products(product_name);
CREATE INDEX IF NOT EXISTS idx_usda_desc ON usda_foods(description);
CREATE INDEX IF NOT EXISTS idx_nutrient_id ON usda_nutrients(nutrient_id);

CREATE TABLE IF NOT EXISTS store_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def import_off(conn, record) -> None:
    payload = record.get("payload") or {}
    conn.execute(
        """INSERT OR REPLACE INTO off_products
           (barcode, fetched_at, product_name, brands, image_url, payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            record["key"],
            record["fetched_at"],
            payload.get("product_name"),
            payload.get("brands"),
            payload.get("image_url"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def import_usda_food(conn, record) -> int:
    payload = record.get("payload") or {}
    fdc_id = payload.get("fdc_id") or record["key"]
    conn.execute(
        """INSERT OR REPLACE INTO usda_foods
           (fdc_id, fetched_at, description, brand_owner, data_type, payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            int(fdc_id),
            record["fetched_at"],
            payload.get("description"),
            payload.get("brand_owner"),
            payload.get("data_type"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )

    nutrients = payload.get("nutrients") or []
    written = 0
    for nutrient in nutrients:
        if not isinstance(nutrient, dict) or nutrient.get("id") is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO usda_nutrients
               (fdc_id, nutrient_id, name, amount, unit) VALUES (?, ?, ?, ?, ?)""",
            (
                int(fdc_id),
                int(nutrient["id"]),
                nutrient.get("name"),
                nutrient.get("amount"),
                nutrient.get("unit"),
            ),
        )
        written += 1
    return written


def import_usda_upc(conn, record) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO usda_upc_map (gtin14, fdc_id, fetched_at)
           VALUES (?, ?, ?)""",
        (record["key"], int(record["payload"]), record["fetched_at"]),
    )


def build(store_dir: Path, db_path: Path, verbose: bool = False) -> dict:
    """Build the database from the corpus, atomically."""
    store.STORE_DIR = store_dir

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build beside the target and swap it in, so a reader never sees a
    # half-built database and a failed run leaves the previous one intact.
    tmp_path = db_path.with_name(f"{db_path.name}.building-{os.getpid()}")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA)

    counts = {"off_products": 0, "usda_foods": 0, "usda_nutrients": 0,
              "usda_upc_map": 0, "skipped": 0}

    for record in store.iter_records():
        namespace = record.get("namespace")
        try:
            if namespace == store.OFF_PRODUCT:
                import_off(conn, record)
                counts["off_products"] += 1
            elif namespace == store.USDA_FOOD:
                counts["usda_nutrients"] += import_usda_food(conn, record)
                counts["usda_foods"] += 1
            elif namespace == store.USDA_UPC:
                import_usda_upc(conn, record)
                counts["usda_upc_map"] += 1
            else:
                counts["skipped"] += 1
                continue
            if verbose:
                logging.info("  %s %s", namespace, record.get("key"))
        except (KeyError, TypeError, ValueError, sqlite3.Error) as e:
            logging.warning("Skipping %s: %s", record.get("_path"), e)
            counts["skipped"] += 1

    for key, value in [
        ("store_dir", str(store_dir)),
        ("schema_version", str(store.SCHEMA_VERSION)),
        ("imported_at", store.utcnow().isoformat()),   # UTC, with the offset
    ]:
        conn.execute(
            "INSERT OR REPLACE INTO store_metadata VALUES (?, ?)", (key, value),
        )

    conn.commit()
    conn.close()
    os.replace(tmp_path, db_path)
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Import the cached upstream responses into SQLite",
    )
    parser.add_argument(
        "--store", type=Path, default=store.STORE_DIR,
        help="Directory holding the JSON response records",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="Output SQLite path",
    )
    parser.add_argument("--verbose", action="store_true", help="Log every record")
    args = parser.parse_args()

    if not args.store.exists():
        logging.error("No response store at %s — nothing to import.", args.store)
        sys.exit(1)

    logging.info("Importing %s -> %s", args.store, args.db)
    counts = build(args.store, args.db, verbose=args.verbose)

    logging.info("Imported:")
    for table, count in counts.items():
        logging.info("  %-16s %d", table, count)


if __name__ == "__main__":
    main()
