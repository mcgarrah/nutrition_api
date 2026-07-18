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
    """A minimal OFF CSV.gz with just the columns the build reads.

    Column names match the real bulk export exactly, including the one that
    tripped us up: OFF's CSV calls the allergens column "allergens", not
    "allergens_tags" (that is the *live API's* field name, for a different
    export). Header/row shape drift here is how that bug would resurface.
    """
    header = ["code", "product_name", "last_modified_t",
              "energy-kcal_100g", "proteins_100g", "sodium_100g",
              "categories_tags", "allergens"]
    rows = [
        # Nutella: real barcode, name, energy, protein, sodium (raw grams).
        ["3017620422003", "Nutella", "1700000000",
         "539", "6.3", "0.0428", "en:spreads,en:sweet-spreads", "en:nuts,en:milk"],
        # No barcode -> skipped.
        ["", "Nameless", "1700000000", "10", "", "", "", ""],
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


# ── Physically-impossible values are nulled, not stored raw ────────────
#
# from_off() (app/core/nutrients.py) already refuses to convert an
# impossible value at lookup time -- but until schema_version 3, the build
# stored the raw figure regardless, so the mirror file itself carried
# numbers already proven impossible (a real one found on data/off.sqlite3:
# a calories_kcal of 1.4x10^16). These tests are the regression guard.

def _export_with(tmp_path, name, extra_header, extra_row):
    """A tiny OFF export with one product, its columns extended by the
    caller -- lets each test add just the field(s) it's testing."""
    gz = tmp_path / name
    header = ["code", "product_name", "last_modified_t",
              "proteins_100g", "sodium_100g", "categories_tags", "allergens"] + extra_header
    row = ["9999999999999", "Test Product", "1700000000",
           "6.3", "0.0428", "", ""] + extra_row
    lines = ["\t".join(header), "\t".join(row)]
    with gzip.open(gz, "wt", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    os.utime(gz, (MODIFIED.timestamp(), MODIFIED.timestamp()))
    return gz


def test_a_physically_impossible_value_is_stored_as_null(tmp_path):
    """The exact shape of a real bug found on the live mirror: a garbage
    calories_kcal (a drink-mix-style per-package figure filed as per-100g,
    per nutrients.py's own documented example) must not survive into the
    database -- other, plausible fields on the same row are unaffected."""
    gz = _export_with(
        tmp_path, "export.csv.gz", ["energy-kcal_100g"], ["151515"])
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    row = sqlite3.connect(out).execute(
        "SELECT calories_kcal, protein, sodium FROM products "
        "WHERE gtin14 = '09999999999999'").fetchone()
    assert row == (None, 6.3, 0.0428)


def test_a_negative_value_is_stored_as_null(tmp_path):
    gz = _export_with(
        tmp_path, "export.csv.gz", ["energy-kcal_100g"], ["-50"])
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    row = sqlite3.connect(out).execute(
        "SELECT calories_kcal FROM products WHERE gtin14 = '09999999999999'"
    ).fetchone()
    assert row == (None,)


def test_a_value_dropped_by_the_energy_floor_check_is_stored_as_null(tmp_path):
    """The Nutella-shaped bug nutrients.py's _reconcile_energy exists for:
    a stated energy far below what the fat alone must contribute. Proves
    the build-time filter goes through the real from_off() (with its
    cross-nutrient checks), not a reimplementation of just the range
    check."""
    gz = _export_with(
        tmp_path, "export.csv.gz",
        ["energy-kcal_100g", "fat_100g"], ["0", "30.9"])
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    row = sqlite3.connect(out).execute(
        "SELECT calories_kcal, fat FROM products WHERE gtin14 = '09999999999999'"
    ).fetchone()
    assert row == (None, 30.9)  # energy dropped; fat itself is plausible, kept


def test_build_maps_the_allergens_column_not_a_nonexistent_tags_column(tmp_path):
    """Regression: TEXT_COLUMNS mapped "allergens" -> "allergens_tags", a column
    the bulk CSV export does not have (that is the live API's field name for
    the same data). cell() returns "" for a missing column rather than
    erroring, so every row silently stored an empty allergens list -- 100% of
    2.24M products, and nothing failed loudly enough to notice.
    """
    gz = tmp_path / "export.csv.gz"
    _tiny_export(gz, MODIFIED)
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    allergens = sqlite3.connect(out).execute(
        "SELECT allergens FROM products WHERE gtin14 = '03017620422003'"
    ).fetchone()[0]
    assert allergens == "en:nuts,en:milk"


# ── Missing-column guard ────────────────────────────────────────────
#
# The allergens bug above was a configured column name that did not exist in
# the export header, silently absorbed by cell() returning "". This is the
# general guard against that class of bug: it does not stop the build (OFF
# does rename/drop columns between exports, and a build script should not
# turn that into an outage), but it must say so, loudly, in the build log.

def test_a_missing_text_column_is_logged_not_silent(caplog):
    header_index = {"code": 0, "product_name": 1}   # "allergens" absent
    with caplog.at_level("WARNING"):
        bod._warn_about_missing_columns(header_index, ["code", "allergens"], "text column")

    assert any("allergens" in r.message for r in caplog.records)
    assert not any("code" in r.message for r in caplog.records)  # code IS present


def test_no_warning_when_every_expected_column_is_present(caplog):
    header_index = {"code": 0, "allergens": 1}
    with caplog.at_level("WARNING"):
        bod._warn_about_missing_columns(header_index, ["code", "allergens"], "text column")

    assert caplog.records == []


def test_build_warns_about_a_genuinely_missing_column(tmp_path, caplog):
    """End to end: a build against a header lacking "allergens" logs it."""
    gz = tmp_path / "export.csv.gz"
    header = ["code", "product_name", "last_modified_t", "energy-kcal_100g"]
    with gzip.open(gz, "wt", encoding="utf-8", newline="") as f:
        f.write("\t".join(header) + "\n")
        f.write("\t".join(["3017620422003", "Nutella", "1700000000", "539"]) + "\n")
    os.utime(gz, (MODIFIED.timestamp(), MODIFIED.timestamp()))

    with caplog.at_level("WARNING"):
        bod.build(gz, tmp_path / "off.sqlite3", "off-2026-07-14")

    assert any("allergens" in r.message for r in caplog.records)


# ── products_fts (name search index) ───────────────────────────────────

def test_build_creates_a_searchable_fts_index(tmp_path):
    gz = tmp_path / "export.csv.gz"
    _tiny_export(gz, MODIFIED)
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    row = sqlite3.connect(out).execute(
        "SELECT gtin14 FROM products_fts WHERE products_fts MATCH ?", ('"nutel"*',)
    ).fetchone()
    assert row == ("03017620422003",)


def test_schema_version_reflects_the_current_build_logic(tmp_path):
    gz = tmp_path / "export.csv.gz"
    _tiny_export(gz, MODIFIED)
    out = tmp_path / "off.sqlite3"

    bod.build(gz, out, "off-2026-07-14")

    meta = dict(sqlite3.connect(out).execute(
        "SELECT key, value FROM off_metadata").fetchall())
    assert meta["schema_version"] == bod.SCHEMA_VERSION
