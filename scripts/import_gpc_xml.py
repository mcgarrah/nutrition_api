#!/usr/bin/env python3
"""
Import GS1 GPC data into SQLite for the Nutrition API.

Uses the gs1_gpc library (GPCDownloader) to fetch the latest GPC XML from GS1,
falling back to the local cached XML. Filters to food-relevant segments only.

Schema uses junction tables to preserve the many-to-many relationships between
bricks and attribute types (the same attribute type can appear on many bricks).

Usage:
    python scripts/import_gpc_xml.py                    # use cached or download
    python scripts/import_gpc_xml.py --download         # force download latest
    python scripts/import_gpc_xml.py --xml data/gpc_november_2024.xml  # explicit file

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import contextlib
import fcntl
import logging
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core import gpc_match  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
DEFAULT_DB = DATA_DIR / "gpc.sqlite3"
LOCAL_XML = DATA_DIR / "gpc_november_2024.xml"

# Food-relevant GPC segments
FOOD_SEGMENTS = {"50000000"}  # Food/Beverage

SCHEMA = """
-- Core hierarchy (1:N relationships, no data loss)
CREATE TABLE IF NOT EXISTS segments (
    segment_code TEXT PRIMARY KEY,
    description  TEXT
);
CREATE TABLE IF NOT EXISTS families (
    family_code  TEXT PRIMARY KEY,
    description  TEXT,
    segment_code TEXT NOT NULL REFERENCES segments(segment_code)
);
CREATE TABLE IF NOT EXISTS classes (
    class_code   TEXT PRIMARY KEY,
    description  TEXT,
    family_code  TEXT NOT NULL REFERENCES families(family_code)
);
CREATE TABLE IF NOT EXISTS bricks (
    brick_code   TEXT PRIMARY KEY,
    description  TEXT,
    class_code   TEXT NOT NULL REFERENCES classes(class_code)
);

-- Attribute types and values are global (shared across bricks)
CREATE TABLE IF NOT EXISTS attribute_types (
    att_type_code TEXT PRIMARY KEY,
    att_type_text TEXT
);
CREATE TABLE IF NOT EXISTS attribute_values (
    att_value_code TEXT PRIMARY KEY,
    att_value_text TEXT
);

-- Junction tables preserve the many-to-many relationships from the XML
CREATE TABLE IF NOT EXISTS brick_attribute_types (
    brick_code    TEXT NOT NULL REFERENCES bricks(brick_code),
    att_type_code TEXT NOT NULL REFERENCES attribute_types(att_type_code),
    PRIMARY KEY (brick_code, att_type_code)
);
CREATE TABLE IF NOT EXISTS attribute_type_values (
    att_type_code  TEXT NOT NULL REFERENCES attribute_types(att_type_code),
    att_value_code TEXT NOT NULL REFERENCES attribute_values(att_value_code),
    PRIMARY KEY (att_type_code, att_value_code)
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_families_segment ON families(segment_code);
CREATE INDEX IF NOT EXISTS idx_classes_family ON classes(family_code);
CREATE INDEX IF NOT EXISTS idx_bricks_class ON bricks(class_code);
CREATE INDEX IF NOT EXISTS idx_bat_brick ON brick_attribute_types(brick_code);
CREATE INDEX IF NOT EXISTS idx_bat_type ON brick_attribute_types(att_type_code);
CREATE INDEX IF NOT EXISTS idx_atv_type ON attribute_type_values(att_type_code);
CREATE INDEX IF NOT EXISTS idx_atv_value ON attribute_type_values(att_value_code);

-- Metadata table for version tracking
CREATE TABLE IF NOT EXISTS gpc_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- FTS5 index over brick descriptions, for the OFF fuzzy category matcher
-- (app/core/gpc_match.py, fuzzy_hierarchy_for_off_categories). A standalone
-- table, not FTS5 "external content": bricks' primary key is TEXT
-- (brick_code), and external-content FTS5 needs an INTEGER rowid to link
-- back to. brick_code is UNINDEXED -- carried through for the join, not
-- itself searched.
CREATE VIRTUAL TABLE IF NOT EXISTS bricks_fts USING fts5(
    brick_code UNINDEXED, description,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def resolve_xml_file(args) -> str:
    """Determine which XML file to use: explicit, download, or cached."""
    if args.xml:
        path = str(args.xml)
        if not Path(path).exists():
            logging.error("XML file not found: %s", path)
            sys.exit(1)
        return path

    try:
        from gs1_gpc.downloader import GPCDownloader
        downloader = GPCDownloader(
            download_dir=str(DATA_DIR / "imports"),
            language_code="en",
        )

        if args.download:
            logging.info("Downloading latest GPC data from GS1...")
            path = downloader.download_latest_gpc_xml()
            if path and Path(path).exists():
                logging.info("Downloaded: %s", path)
                return path
            logging.warning("Download failed, falling back to cached files.")

        cached = downloader.find_latest_xml_file()
        if cached and Path(cached).exists():
            logging.info("Using cached GPC XML: %s", cached)
            return cached

    except ImportError:
        logging.warning("gs1_gpc library not installed. Using local XML file.")
    except Exception as e:
        logging.warning("GPCDownloader error: %s. Using local XML file.", e)

    if LOCAL_XML.exists():
        logging.info("Using local XML: %s", LOCAL_XML)
        return str(LOCAL_XML)

    logging.error("No GPC XML file available. Provide one with --xml or install gs1-gpc.")
    sys.exit(1)


UNKNOWN_VERSION = "unknown"


def extract_version_from_path(xml_path: str, xml_date: str = UNKNOWN_VERSION) -> str:
    """Extract a sortable YYYYMMDD version from the filename or the XML date.

    GPCDownloader names files like 'en-v20251127.xml' -> version '20251127'.
    For other filenames we fall back to the XML's dateUtc attribute, which GS1
    publishes as D/M/YYYY — e.g. '27/11/2025' is 27 November 2025, and the
    bundled November-2024 file carries '2/12/2024' meaning 2 December 2024.
    """
    name = Path(xml_path).stem
    if "-v" in name:
        return name.split("-v", 1)[1]  # '20251127'
    elif "-" in name:
        parts = name.split("-", 1)[1]
        if parts.isdigit():
            return parts

    if xml_date and xml_date != UNKNOWN_VERSION:
        import datetime
        try:
            dt = datetime.datetime.strptime(xml_date.strip(), "%d/%m/%Y")
            return dt.strftime("%Y%m%d")
        except (ValueError, AttributeError):
            pass
    return UNKNOWN_VERSION


def is_remote_newer(remote: str | None, stored: str | None) -> bool:
    """Decide whether GS1's version supersedes the one we already imported.

    Both are YYYYMMDD strings, so a lexical compare orders them correctly —
    but only once we have refused to compare anything that isn't one. An
    unparseable stored version ("unknown") must mean "we don't know what we
    have, so take the remote", never "ours sorts higher than any date".
    """
    if not remote:
        return False
    remote = remote.lstrip("v")
    if not remote.isdigit():
        return False
    if not stored:
        return True
    stored = stored.lstrip("v")
    if not stored.isdigit():
        return True  # our version is unusable — trust the remote
    return remote > stored


def get_stored_version(db_path: Path) -> str | None:
    """Read the GPC version from an existing database, or None if no DB."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT value FROM gpc_metadata WHERE key = 'gpc_version'"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# How often to check GS1 for newer data (seconds). Default: 24 hours.
VERSION_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# Network timeout for GS1 API calls (seconds).
GS1_CHECK_TIMEOUT_SECONDS = 15


def get_last_version_check(db_path: Path) -> str | None:
    """Read the timestamp of the last remote version check, or None."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT value FROM gpc_metadata WHERE key = 'last_version_check'"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def set_last_version_check(db_path: Path) -> None:
    """Record the current time as the last version check timestamp."""
    import datetime
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO gpc_metadata VALUES (?, ?)",
            ("last_version_check",
             datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # non-critical — worst case we check again next boot


def should_check_remote(db_path: Path) -> bool:
    """Return True if enough time has passed since the last remote check."""
    import datetime
    last_check = get_last_version_check(db_path)
    if not last_check:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last_check)
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds()
        if elapsed < VERSION_CHECK_INTERVAL_SECONDS:
            logging.info(
                "Last GS1 version check was %.0f minutes ago (interval: %d hours). Skipping.",
                elapsed / 60,
                VERSION_CHECK_INTERVAL_SECONDS // 3600,
            )
            return False
        return True
    except (ValueError, TypeError):
        return True


def get_latest_remote_version() -> str | None:
    """Query GS1 via gpcc for the latest publication version string.

    Uses a timeout to avoid blocking startup if GS1 is slow or unreachable.
    """
    try:
        import asyncio
        # gpcc re-exports these from its package root and lists them in
        # __all__ — reaching into gpcc._crawlers bought nothing but the risk
        # of a private module moving under us.
        from gpcc import get_language, get_publications

        async def _check():
            lang = await asyncio.wait_for(
                get_language("en"),
                timeout=GS1_CHECK_TIMEOUT_SECONDS,
            )
            pubs = await asyncio.wait_for(
                get_publications(lang),
                timeout=GS1_CHECK_TIMEOUT_SECONDS,
            )
            return pubs[0].version if pubs else None

        return asyncio.run(_check())
    except asyncio.TimeoutError:
        logging.warning(
            "GS1 version check timed out after %d seconds.",
            GS1_CHECK_TIMEOUT_SECONDS,
        )
        return None
    except Exception as e:
        logging.warning("Could not check remote GPC version: %s", e)
        return None


def import_food_gpc(xml_path: str, db_path: Path) -> dict:
    """Parse GPC XML, filter to food segments, insert with correct many-to-many schema."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Extract version from XML root attributes and filename
    xml_date = root.get("dateUtc", "unknown")
    gpc_version = extract_version_from_path(xml_path, xml_date)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build into a temporary file beside the target, then swap it in atomically.
    #
    # Deleting the live database and rebuilding it in place was destructive in
    # three ways: a worker already serving requests kept an open handle to the
    # unlinked inode, a second importer writing the same file hit "disk I/O
    # error", and a crash part-way through left a half-built database that the
    # next boot happily mistook for a good one. os.replace is atomic on POSIX
    # within a filesystem: readers see either the old database or the new one,
    # never a partial one.
    tmp_path = db_path.with_name(f"{db_path.name}.building-{os.getpid()}")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)

    counts = {
        "segments": 0, "families": 0, "classes": 0, "bricks": 0,
        "attribute_types": 0, "attribute_values": 0,
        "brick_attribute_types": 0, "attribute_type_values": 0,
    }

    for segment in root.findall("segment"):
        seg_code = segment.get("code")
        if seg_code not in FOOD_SEGMENTS:
            continue

        conn.execute(
            "INSERT OR IGNORE INTO segments VALUES (?, ?)",
            (seg_code, segment.get("text")),
        )
        counts["segments"] += 1

        for family in segment.findall("family"):
            fam_code = family.get("code")
            conn.execute(
                "INSERT OR IGNORE INTO families VALUES (?, ?, ?)",
                (fam_code, family.get("text"), seg_code),
            )
            counts["families"] += 1

            for cls in family.findall("class"):
                cls_code = cls.get("code")
                conn.execute(
                    "INSERT OR IGNORE INTO classes VALUES (?, ?, ?)",
                    (cls_code, cls.get("text"), fam_code),
                )
                counts["classes"] += 1

                for brick in cls.findall("brick"):
                    brk_code = brick.get("code")
                    conn.execute(
                        "INSERT OR IGNORE INTO bricks VALUES (?, ?, ?)",
                        (brk_code, brick.get("text"), cls_code),
                    )
                    counts["bricks"] += 1

                    for att_type in brick.findall("attType"):
                        at_code = att_type.get("code")
                        conn.execute(
                            "INSERT OR IGNORE INTO attribute_types VALUES (?, ?)",
                            (at_code, att_type.get("text")),
                        )
                        counts["attribute_types"] += 1
                        conn.execute(
                            "INSERT OR IGNORE INTO brick_attribute_types VALUES (?, ?)",
                            (brk_code, at_code),
                        )
                        counts["brick_attribute_types"] += 1

                        for att_val in att_type.findall("attValue"):
                            av_code = att_val.get("code")
                            conn.execute(
                                "INSERT OR IGNORE INTO attribute_values VALUES (?, ?)",
                                (av_code, att_val.get("text")),
                            )
                            counts["attribute_values"] += 1
                            conn.execute(
                                "INSERT OR IGNORE INTO attribute_type_values VALUES (?, ?)",
                                (at_code, av_code),
                            )
                            counts["attribute_type_values"] += 1

    conn.execute("""INSERT INTO bricks_fts (brick_code, description)
        SELECT brick_code, description FROM bricks""")

    # Store metadata
    import datetime
    for key, value in [
        ("xml_date", xml_date),
        ("xml_source", xml_path),
        ("food_segments", ",".join(sorted(FOOD_SEGMENTS))),
        ("gpc_version", gpc_version),
        ("import_timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat()),
    ]:
        conn.execute(
            "INSERT OR REPLACE INTO gpc_metadata VALUES (?, ?)",
            (key, value),
        )

    conn.commit()
    conn.close()

    # Atomic swap: the target either is the old database or is the new one.
    os.replace(tmp_path, db_path)

    return counts


def check_curated_codes(db_path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """Resolve every code in gpc_match.py's four curated tables
    (FDC_CATEGORY_TO_BRICK/_CLASS, OFF_TAG_TO_BRICK/_CLASS) against the
    just-imported database.

    A curated code the taxonomy no longer has is a silent regression
    waiting to happen: hierarchy_for_brick()/hierarchy_for_class() already
    return [] for an unknown code, by design -- correct for a *lookup*
    code (a real "no GPC classification found" case) -- but a *curated*
    code degrading the exact same way is indistinguishable from "no
    curated entry exists for this category/tag" to anything downstream.
    Run right after a successful rebuild so a code GS1 retired or
    renumbered between taxonomy versions is caught here, not discovered
    later as an unexplained drop in curated coverage. See PLAN.md item 9.

    Returns {"bricks": [(table_name, key, code), ...], "classes": [...]}
    -- both lists empty means every curated code still resolves.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        valid_bricks = {r[0] for r in conn.execute("SELECT brick_code FROM bricks")}
        valid_classes = {r[0] for r in conn.execute("SELECT class_code FROM classes")}
    finally:
        conn.close()

    stale: dict[str, list[tuple[str, str, str]]] = {"bricks": [], "classes": []}
    tables = [
        ("FDC_CATEGORY_TO_BRICK", gpc_match.FDC_CATEGORY_TO_BRICK, valid_bricks, "bricks"),
        ("FDC_CATEGORY_TO_CLASS", gpc_match.FDC_CATEGORY_TO_CLASS, valid_classes, "classes"),
        ("OFF_TAG_TO_BRICK", gpc_match.OFF_TAG_TO_BRICK, valid_bricks, "bricks"),
        ("OFF_TAG_TO_CLASS", gpc_match.OFF_TAG_TO_CLASS, valid_classes, "classes"),
    ]
    for table_name, mapping, valid_codes, kind in tables:
        for key, code in mapping.items():
            if code not in valid_codes:
                stale[kind].append((table_name, key, code))
    return stale


@contextlib.contextmanager
def import_lock(db_path: Path):
    """Serialize importers across processes.

    Every uvicorn worker runs the startup lifespan, so `--workers 2` means two
    processes reach this code at the same boot. Both would decide an import is
    due, and both would build the same database — which in practice means one
    of them dies with "disk I/O error" while the other is mid-write.

    The lock is held for the whole decide-and-import sequence, and it blocks
    rather than skipping: a worker that arrives second must not race ahead and
    start serving before the database it needs actually exists. It waits, then
    re-checks and finds the work already done.
    """
    lock_path = db_path.with_name(f"{db_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="Import GS1 GPC food data into SQLite")
    parser.add_argument("--xml", type=Path, help="Path to GPC XML file (overrides download)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Output SQLite path")
    parser.add_argument("--download", action="store_true", help="Download latest from GS1")
    parser.add_argument(
        "--auto-update", action="store_true",
        help="Check GS1 for newer version; skip import if local is current",
    )
    args = parser.parse_args()

    # Held for the whole decide-and-import sequence: two uvicorn workers boot
    # at once, and without this both conclude an import is due and then race to
    # build the same file.
    with import_lock(args.db):
        _run_import(args)


def _run_import(args) -> None:
    # Auto-update mode: compare local version to remote, skip if current
    if args.auto_update:
        stored = get_stored_version(args.db)
        if stored:
            # Rate-limit: skip remote check if we checked recently
            if not should_check_remote(args.db):
                return

            remote = get_latest_remote_version()
            # Record that we checked, regardless of outcome
            set_last_version_check(args.db)

            if remote and not is_remote_newer(remote, stored):
                logging.info(
                    "GPC data is current (local=%s, remote=%s). No update needed.",
                    stored, remote,
                )
                return
            if remote:
                logging.info(
                    "Newer GPC data available (local=%s, remote=%s). Updating...",
                    stored, remote,
                )
                args.download = True  # force download of newer version
            else:
                logging.info(
                    "Could not check remote version (timeout or error). "
                    "Using existing data."
                )
                return
        else:
            logging.info("No existing GPC database. Building from available data.")

    xml_path = resolve_xml_file(args)
    logging.info("Importing %s -> %s (food segments only)", xml_path, args.db)

    counts = import_food_gpc(xml_path, args.db)

    total = sum(counts.values())
    logging.info("Imported %d records:", total)
    for table, count in counts.items():
        logging.info("  %s: %d", table, count)

    # Report unique vs occurrence counts
    conn = sqlite3.connect(args.db)
    for table in ["attribute_types", "attribute_values"]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        logging.info("  %s (unique rows in DB): %d", table, row[0])
    for table in ["brick_attribute_types", "attribute_type_values"]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        logging.info("  %s (junction rows): %d", table, row[0])
    conn.close()

    # PLAN.md item 9: confirm every curated GPC code still resolves against
    # *this* taxonomy version. Logged, not fatal -- this script's exit code
    # already means "did the import itself succeed," and app/main.py's
    # startup lifespan treats a non-zero exit as an auto-update failure,
    # falling back to the previous database. A stale curated code is a
    # curation-maintenance signal, not an import failure, so it must not be
    # reported (or handled) the same way.
    stale = check_curated_codes(args.db)
    stale_count = len(stale["bricks"]) + len(stale["classes"])
    if stale_count:
        logging.warning(
            "%d curated GPC code(s) no longer resolve against this taxonomy "
            "version -- silently degrading to an unmatched category/tag:",
            stale_count,
        )
        for kind in ("bricks", "classes"):
            for table_name, key, code in stale[kind]:
                logging.warning("  %s[%r] -> %s (missing from %s)", table_name, key, code, kind)
    else:
        logging.info("All curated GPC codes resolve against this taxonomy version.")


if __name__ == "__main__":
    main()
