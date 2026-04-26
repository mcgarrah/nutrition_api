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
import logging
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
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


def import_food_gpc(xml_path: str, db_path: Path) -> dict:
    """Parse GPC XML, filter to food segments, insert with correct many-to-many schema."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Extract version from XML root attributes
    xml_date = root.get("dateUtc", "unknown")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
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

    # Store metadata
    conn.execute(
        "INSERT OR REPLACE INTO gpc_metadata VALUES (?, ?)",
        ("xml_date", xml_date),
    )
    conn.execute(
        "INSERT OR REPLACE INTO gpc_metadata VALUES (?, ?)",
        ("xml_source", xml_path),
    )
    conn.execute(
        "INSERT OR REPLACE INTO gpc_metadata VALUES (?, ?)",
        ("food_segments", ",".join(sorted(FOOD_SEGMENTS))),
    )

    conn.commit()
    conn.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description="Import GS1 GPC food data into SQLite")
    parser.add_argument("--xml", type=Path, help="Path to GPC XML file (overrides download)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Output SQLite path")
    parser.add_argument("--download", action="store_true", help="Download latest from GS1")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
