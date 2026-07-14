"""
Tests for the local copy of the USDA FDC branded dataset.

The local tier answers barcode lookups from a bulk import instead of the FDC
API. What has to hold:

  * a hit returns a record the orchestrator cannot distinguish from an upstream
    one — same shape, same nutrient ids, same units — because a second, quietly
    diverging copy of the mapping rules is exactly the class of bug that gave us
    cheddar at 1710 kcal;
  * a miss falls through to the API rather than being reported as "no such
    product", since the copy is only as new as the last dataset;
  * a broken or absent copy degrades to the API instead of failing the request.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import lzma
import sqlite3

import pytest

from app.core import fdc_local
from app.core import orchestrator
from app.core.nutrients import NUTRIENTS

FIELDS = [spec.field for spec in NUTRIENTS]

# One real product, and one whose vitamin D came from FDC's IU id — stored in
# micrograms, because the conversion happens once, at import.
ROWS = [
    {
        "gtin14": "00072940755050", "fdc_id": 344604, "published": "2026-02-19",
        "description": "Italian Diced Tomatoes", "brand_owner": "Red Gold",
        "brand_name": None, "ingredients": "Tomatoes, Tomato Juice, Salt",
        "serving_size": 123.0, "serving_size_unit": "g",
        "household_serving": "1/2 cup", "category": "Canned Vegetables",
        "calories_kcal": 24.0, "protein": 0.81, "fat": 0.41, "sodium": 203.0,
        "vitamin_d": 0.0,
    },
    {
        "gtin14": "00099447210127", "fdc_id": 555001, "published": "2025-06-01",
        "description": "Fortified Whole Milk", "brand_owner": "Dairy Co",
        "brand_name": None, "ingredients": "Milk, Vitamin D3",
        "serving_size": 240.0, "serving_size_unit": "ml",
        "household_serving": "1 cup", "category": "Milk",
        "calories_kcal": 61.0, "protein": 3.2, "fat": 3.3, "sodium": 43.0,
        "vitamin_d": 10.0,          # 400 IU upstream, converted at import
    },
]


def build_db(path):
    """A fixture database with the production schema."""
    declared = ", ".join(f"{f} REAL" for f in FIELDS)
    db = sqlite3.connect(path)
    db.execute(f"""CREATE TABLE foods (
        gtin14 TEXT PRIMARY KEY, fdc_id INTEGER NOT NULL, published TEXT,
        description TEXT, brand_owner TEXT, brand_name TEXT, ingredients TEXT,
        serving_size REAL, serving_size_unit TEXT, household_serving TEXT,
        category TEXT, {declared}) WITHOUT ROWID""")
    db.execute("CREATE TABLE fdc_metadata (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO fdc_metadata VALUES (?,?)", [
        ("dataset", "FoodData_Central_branded_food_csv_2026-04-30"),
        ("barcodes", str(len(ROWS))),
        ("import_timestamp", "2026-07-14T00:00:00+00:00"),
        ("schema_version", "1"),
    ])
    columns = ["gtin14", "fdc_id", "published", "description", "brand_owner",
               "brand_name", "ingredients", "serving_size", "serving_size_unit",
               "household_serving", "category"] + FIELDS
    placeholders = ",".join("?" * len(columns))
    db.executemany(
        f"INSERT INTO foods ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row.get(c) for c in columns) for row in ROWS],
    )
    db.commit()
    db.close()
    return path


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    path = build_db(tmp_path / "fdc.sqlite3")
    monkeypatch.setattr(fdc_local, "DB_PATH", path)
    monkeypatch.setattr(fdc_local, "_db", None)
    monkeypatch.setattr(fdc_local, "_metadata", {})
    return path


# ── Lookups ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_known_barcode_is_found(local_db):
    record = await fdc_local.get_by_gtin("00072940755050")

    assert record["fdc_id"] == 344604
    assert record["description"] == "Italian Diced Tomatoes"
    assert record["brand_owner"] == "Red Gold"


@pytest.mark.asyncio
async def test_an_unknown_barcode_returns_none_not_an_error(local_db):
    """A miss means "ask the API", not "no such product"."""
    assert await fdc_local.get_by_gtin("00000000000000") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("written_as", [
    "0072940755050",      # GTIN-13
    "072940755050",       # GTIN-12
    "00072940755050",     # GTIN-14
])
async def test_a_barcode_is_found_at_any_zero_padding(local_db, written_as):
    """The same identifier, padded differently. Scanners disagree; we must not."""
    record = await fdc_local.get_by_gtin(written_as)

    assert record["fdc_id"] == 344604


@pytest.mark.asyncio
async def test_junk_input_is_rejected_without_touching_the_database(local_db):
    assert await fdc_local.get_by_gtin("not-a-barcode") is None
    assert await fdc_local.get_by_gtin("") is None


# ── The record is shaped like an upstream one ─────────────────────────

@pytest.mark.asyncio
async def test_nutrients_come_back_as_an_fdc_style_list_keyed_by_id(local_db):
    """Not a dict keyed by name.

    The orchestrator feeds this straight to from_usda, which selects by id
    precisely so that FDC's two "Energy" entries cannot collapse into one
    another. A local record that used names would reintroduce the bug.
    """
    record = await fdc_local.get_by_gtin("00072940755050")

    assert isinstance(record["nutrients"], list)
    by_id = {n["id"]: n for n in record["nutrients"]}
    assert by_id[1008]["amount"] == 24.0      # energy, the kcal id
    assert by_id[1008]["unit"] == "KCAL"
    assert by_id[1093]["amount"] == 203.0     # sodium, mg


@pytest.mark.asyncio
async def test_the_orchestrator_maps_a_local_record_exactly_as_an_upstream_one(local_db):
    """The whole point of the shared shape: one mapping, not two."""
    from app.core.nutrients import from_usda

    record = await fdc_local.get_by_gtin("00099447210127")
    values = from_usda(record["nutrients"])

    assert values["calories_kcal"] == 61.0
    assert values["protein"] == 3.2
    # 400 IU upstream. Converted once, at import — and *not* converted twice.
    assert values["vitamin_d"] == 10.0


@pytest.mark.asyncio
async def test_absent_nutrients_are_omitted_not_reported_as_zero(local_db):
    """A nutrient FDC never published is unknown, which is not the same as none."""
    record = await fdc_local.get_by_gtin("00072940755050")
    ids = {n["id"] for n in record["nutrients"]}

    assert 1005 not in ids          # carbohydrates: NULL in the row
    assert 1008 in ids


# ── Degradation ───────────────────────────────────────────────────────

def test_no_local_database_means_no_local_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(fdc_local, "DB_PATH", tmp_path / "nothing.sqlite3")

    assert fdc_local.is_available() is False
    assert fdc_local.stats()["status"] == "absent"


@pytest.mark.asyncio
async def test_a_corrupt_database_degrades_instead_of_failing(tmp_path, monkeypatch):
    """Better to ask the API than to 500. A truncated file must not be fatal."""
    broken = tmp_path / "fdc.sqlite3"
    broken.write_bytes(b"this is not a database")
    monkeypatch.setattr(fdc_local, "DB_PATH", broken)
    monkeypatch.setattr(fdc_local, "_db", None)

    assert await fdc_local.get_by_gtin("00072940755050") is None


@pytest.mark.asyncio
async def test_the_local_tier_can_be_switched_off(local_db, monkeypatch):
    monkeypatch.setattr(fdc_local, "ENABLED", False)

    assert fdc_local.is_available() is False
    assert await fdc_local.get_by_gtin("00072940755050") is None


# ── The archive ───────────────────────────────────────────────────────

def test_the_archive_is_expanded_on_startup(tmp_path, monkeypatch):
    """The database is too big for git; the ~28 MB xz is what we keep."""
    source = build_db(tmp_path / "source.sqlite3")
    archive = tmp_path / "fdc.sqlite3.xz"
    with open(source, "rb") as src, lzma.open(archive, "wb") as dst:
        dst.write(src.read())
    target = tmp_path / "fdc.sqlite3"
    monkeypatch.setattr(fdc_local, "DB_PATH", target)
    monkeypatch.setattr(fdc_local, "ARCHIVE_PATH", archive)

    assert fdc_local.ensure_database() is True
    assert target.exists()
    assert sqlite3.connect(target).execute(
        "SELECT COUNT(*) FROM foods").fetchone()[0] == len(ROWS)


def test_an_existing_database_is_not_re_expanded(local_db, monkeypatch):
    """Startup must not pay to rebuild what is already there."""
    monkeypatch.setattr(fdc_local, "ARCHIVE_PATH", local_db.parent / "missing.xz")
    before = local_db.stat().st_mtime_ns

    assert fdc_local.ensure_database() is True
    assert local_db.stat().st_mtime_ns == before


def test_a_corrupt_archive_leaves_no_half_written_database(tmp_path, monkeypatch):
    """An interrupted expansion must not leave a file we would then serve."""
    archive = tmp_path / "fdc.sqlite3.xz"
    archive.write_bytes(b"\xfd7zXZ\x00truncated garbage")
    target = tmp_path / "fdc.sqlite3"
    monkeypatch.setattr(fdc_local, "DB_PATH", target)
    monkeypatch.setattr(fdc_local, "ARCHIVE_PATH", archive)

    assert fdc_local.ensure_database() is False
    assert not target.exists()
    assert not target.with_suffix(".expanding").exists()


# ── Wiring: local first, API for the rest ─────────────────────────────

@pytest.mark.asyncio
async def test_a_local_hit_never_calls_the_api(local_db, monkeypatch):
    """The reason the local copy exists: no key, no token, no round trip."""
    called = []

    async def explode(barcode):
        called.append(barcode)
        raise AssertionError("the API must not be called for a local hit")

    monkeypatch.setattr(orchestrator.usda_fdc, "search_by_upc", explode)

    data, _ = await orchestrator._fetch_usda("00072940755050")

    assert data["fdc_id"] == 344604
    assert called == []


@pytest.mark.asyncio
async def test_a_local_miss_falls_through_to_the_api(local_db, monkeypatch):
    """New products are newer than the dataset. They still have to work."""
    async def upstream(barcode):
        return {"fdc_id": 999, "description": "BRAND NEW SNACK", "nutrients": []}

    monkeypatch.setattr(orchestrator.usda_fdc, "search_by_upc", upstream)

    data, _ = await orchestrator._fetch_usda("00000000000000")

    assert data["fdc_id"] == 999


@pytest.mark.asyncio
async def test_a_broken_local_copy_still_lets_the_api_answer(tmp_path, monkeypatch):
    broken = tmp_path / "fdc.sqlite3"
    broken.write_bytes(b"not a database")
    monkeypatch.setattr(fdc_local, "DB_PATH", broken)
    monkeypatch.setattr(fdc_local, "_db", None)

    async def upstream(barcode):
        return {"fdc_id": 42, "description": "FROM THE API", "nutrients": []}

    monkeypatch.setattr(orchestrator.usda_fdc, "search_by_upc", upstream)

    data, _ = await orchestrator._fetch_usda("00072940755050")

    assert data["fdc_id"] == 42


def test_health_names_the_dataset_before_any_lookup_has_happened(local_db):
    """/health is polled on a cold process. "dataset: null" tells an operator
    nothing about how stale the copy is, which is the one thing they need."""
    reported = fdc_local.stats()

    assert reported["status"] == "ok"
    assert reported["dataset"] == "FoodData_Central_branded_food_csv_2026-04-30"
    assert reported["barcodes"] == len(ROWS)
    assert reported["imported_at"] == "2026-07-14T00:00:00+00:00"


def test_health_reports_an_unreadable_database_as_an_error(tmp_path, monkeypatch):
    broken = tmp_path / "fdc.sqlite3"
    broken.write_bytes(b"not a database")
    monkeypatch.setattr(fdc_local, "DB_PATH", broken)
    monkeypatch.setattr(fdc_local, "_metadata", {})

    assert fdc_local.stats()["status"] == "error"
