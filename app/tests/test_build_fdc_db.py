"""
Tests for build_fdc_db.py's persisted upstream-vs-mirrored exclusion counts.

PLAN.md item 12: what fraction of the branded_food.csv export we actually kept,
and why, must survive past the build's own _step() log line and into
fdc_metadata, the same place dataset provenance already lives.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import csv
import io
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import build_fdc_db as bfd  # noqa: E402

BASE = "FoodData_Central_branded_food_csv_2026-07-14/"
MODIFIED = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _csv_bytes(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _tiny_export(path, branded_rows):
    """A minimal FDC bulk zip: one nutrient definition, one food per branded
    row, and the branded rows a caller wants to exercise.

    `branded_rows` is a list of (fdc_id, gtin_upc) pairs. Each gets a matching
    food.csv row (so the join in _build_served_table has something to find)
    and one Energy nutrient reading, just enough to flow end to end.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(BASE + "nutrient.csv", _csv_bytes(
            ["id", "name", "unit_name"], [["1008", "Energy", "KCAL"]]))
        zf.writestr(BASE + "food.csv", _csv_bytes(
            ["fdc_id", "description", "publication_date"],
            [[str(fdc_id), f"Food {fdc_id}", "2026-06-01"]
             for fdc_id, _ in branded_rows]))
        zf.writestr(BASE + "branded_food.csv", _csv_bytes(
            list(bfd.BRANDED_COLUMNS),
            [[str(fdc_id), gtin_upc, "Acme", "Acme Brand", "sugar",
              "100", "g", "1 bag", "Snacks"]
             for fdc_id, gtin_upc in branded_rows]))
        zf.writestr(BASE + "food_nutrient.csv", _csv_bytes(
            ["fdc_id", "nutrient_id", "amount"],
            [[str(fdc_id), "1008", "250"] for fdc_id, _ in branded_rows]))
    os.utime(path, (MODIFIED.timestamp(), MODIFIED.timestamp()))


def test_build_records_upstream_vs_mirrored_exclusion_counts(tmp_path):
    """One row with a usable GTIN, one without -- the without is excluded
    outright, not deduped."""
    zip_path = tmp_path / "export.zip"
    _tiny_export(zip_path, [
        (1001, "0028400589279"),  # usable barcode -> kept
        (1002, ""),               # no barcode -> rejected
    ])
    out = tmp_path / "fdc.sqlite3"

    stats = bfd.build(zip_path, out, "2026-07-14")

    meta = dict(sqlite3.connect(out).execute(
        "SELECT key, value FROM fdc_metadata").fetchall())
    assert meta["rows_read"] == "2"      # both branded_food.csv rows
    assert meta["excluded"] == "1"       # the row with no usable GTIN
    assert meta["deduped"] == "0"        # no colliding barcodes here
    assert stats["barcodes"] == 1


def test_deduped_count_reflects_superseded_revisions_folding_in(tmp_path):
    """Two fdc_ids publishing the same barcode collapse to one served row --
    excluded stays 0 (both had a usable GTIN), deduped counts the collapse."""
    zip_path = tmp_path / "export.zip"
    _tiny_export(zip_path, [
        (2001, "0028400589279"),  # older revision of the same barcode
        (2002, "0028400589279"),  # newer revision -- defines the product
    ])
    out = tmp_path / "fdc.sqlite3"

    stats = bfd.build(zip_path, out, "2026-07-14")

    meta = dict(sqlite3.connect(out).execute(
        "SELECT key, value FROM fdc_metadata").fetchall())
    assert meta["rows_read"] == "2"
    assert meta["excluded"] == "0"
    assert meta["deduped"] == "1"        # two revisions, one surviving barcode
    assert stats["barcodes"] == 1
