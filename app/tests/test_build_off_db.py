"""
Tests for the Open Food Facts importer, focused on how downloads are dated.

OFF publishes one rolling URL, overwritten daily. To hold several days side by
side for comparison, a download is named for the export's *own* Last-Modified
time — not the moment we fetched it — kept rather than overwritten, and stamped
so the file's mtime reflects when OFF built it. The build records that same
timestamp in the database, pinning it to an exact upstream export.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import gzip
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import build_off_db as bod  # noqa: E402

MODIFIED = datetime(2026, 7, 14, 11, 26, 59, tzinfo=timezone.utc)


# ── Naming ────────────────────────────────────────────────────────────

def test_the_download_is_named_for_the_exports_own_timestamp():
    """Same export, same name — for everyone, whenever they fetch it."""
    name = bod.dated_download_name(MODIFIED)

    assert name == "off-products-2026-07-14T112659Z.csv.gz"


def test_two_different_exports_get_two_different_names():
    """This is what lets days accumulate instead of overwriting."""
    day1 = bod.dated_download_name(MODIFIED)
    day2 = bod.dated_download_name(MODIFIED.replace(day=15))

    assert day1 != day2
    assert "2026-07-14" in day1 and "2026-07-15" in day2


def test_a_missing_timestamp_still_produces_a_usable_name():
    """If OFF omits Last-Modified we fall back to now, never to a crash."""
    name = bod.dated_download_name(None)

    assert name.startswith("off-products-") and name.endswith(".csv.gz")


# ── Stamping ──────────────────────────────────────────────────────────

def test_a_finished_download_carries_the_content_time_and_is_world_readable(tmp_path):
    partial = tmp_path / "partial.tmp"
    partial.write_bytes(b"payload")
    dest = tmp_path / bod.dated_download_name(MODIFIED)

    bod._install_download(partial, dest, MODIFIED)

    assert dest.exists() and not partial.exists()
    assert oct(dest.stat().st_mode)[-3:] == "644"
    assert int(dest.stat().st_mtime) == int(MODIFIED.timestamp())


# ── The build records the source timestamp ────────────────────────────

def _tiny_export(path, modified):
    """A minimal OFF CSV.gz with just the columns the build reads."""
    header = ["code", "product_name", "last_modified_t",
              "energy-kcal_100g", "proteins_100g", "sodium_100g", "categories_tags"]
    rows = [
        # Nutella: real barcode, name, energy, protein, sodium (raw grams).
        ["3017620422003", "Nutella", "1700000000",
         "539", "6.3", "0.0428", "en:spreads,en:sweet-spreads"],
        # No barcode -> skipped.
        ["", "Nameless", "1700000000", "10", "", "", ""],
    ]
    lines = ["\t".join(header)] + ["\t".join(r) for r in rows]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    os.utime(path, (modified.timestamp(), modified.timestamp()))


def test_build_records_the_source_export_timestamp(tmp_path):
    gz = tmp_path / "off-products-2026-07-14T112659Z.csv.gz"
    _tiny_export(gz, MODIFIED)
    out = tmp_path / "off.sqlite3"

    stats = bod.build(gz, out, "off-2026-07-14")

    assert stats["products"] == 1                # the nameless row was skipped
    meta = dict(sqlite3.connect(out).execute(
        "SELECT key, value FROM off_metadata").fetchall())
    assert meta["source_modified"] == "2026-07-14T11:26:59+00:00"
    assert meta["dataset"] == "off-2026-07-14"


def test_build_stores_nutrients_raw_for_conversion_at_lookup(tmp_path):
    """Sodium goes in as OFF's 0.0428 g, not a pre-multiplied 42.8 mg."""
    gz = tmp_path / "export.csv.gz"
    _tiny_export(gz, MODIFIED)
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    row = sqlite3.connect(out).execute(
        "SELECT calories_kcal, protein, sodium FROM products "
        "WHERE gtin14 = '03017620422003'").fetchone()
    assert row == (539.0, 6.3, 0.0428)
