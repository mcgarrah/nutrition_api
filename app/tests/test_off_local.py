"""
Tests for the local copy of the Open Food Facts export.

The local tier answers product lookups from the daily bulk export instead of the
OFF API. What has to hold:

  * a hit returns a record the orchestrator cannot distinguish from a live one —
    same shape, same raw grams under nutrients_per_100g — so from_off does the
    unit conversion once, at the same point in the pipeline, and the two tiers
    cannot disagree;
  * a miss falls through to the API, since the copy holds only the products the
    last export carried and only those complete enough to import;
  * a broken or absent copy degrades to the API instead of failing the request.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import lzma
import sqlite3

import pytest

from app.core import off_local
from app.core import orchestrator
from app.core.nutrients import NUTRIENTS, from_off

FIELDS = [spec.field for spec in NUTRIENTS]

# Sodium is stored raw, in OFF's grams (0.4 g), not the 400 mg a label shows —
# from_off multiplies by 1000 at lookup. Storing 400 would be the double-convert
# bug this design exists to avoid.
ROWS = [
    {
        "gtin14": "04963406021372", "modified": 1_700_000_000,
        "product_name": "Coca-Cola Classic", "brands": "Coca-Cola",
        "image_url": "https://images.example.org/coke.jpg",
        "ingredients_text": "Carbonated water, high fructose corn syrup",
        "quantity": "330 ml", "serving_size": "330 ml",
        "categories": "en:beverages,en:carbonated-drinks",
        "allergens": "", "labels": "en:no-lactose",
        "calories_kcal": 44.0, "sugars": 10.6, "sodium": 0.004,   # 4 mg, raw
    },
    {
        "gtin14": "00072940755050", "modified": 1_710_000_000,
        "product_name": "Diced Tomatoes", "brands": "Red Gold",
        "image_url": None, "ingredients_text": "Tomatoes, salt",
        "quantity": "411 g", "serving_size": "123 g",
        "categories": "en:canned-tomatoes", "allergens": "", "labels": "",
        "calories_kcal": 24.0, "protein": 0.81,
    },
]


def build_db(path):
    text_cols = ("product_name TEXT, brands TEXT, image_url TEXT, "
                 "ingredients_text TEXT, quantity TEXT, serving_size TEXT, "
                 "categories TEXT, allergens TEXT, labels TEXT")
    nutrient_cols = ", ".join(f"{f} REAL" for f in FIELDS)
    db = sqlite3.connect(path)
    db.execute(f"""CREATE TABLE products (
        gtin14 TEXT PRIMARY KEY, modified INTEGER, {text_cols}, {nutrient_cols}
    ) WITHOUT ROWID""")
    db.execute("CREATE TABLE off_metadata (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO off_metadata VALUES (?,?)", [
        ("dataset", "off-2026-07-14"),
        ("products", str(len(ROWS))),
        ("import_timestamp", "2026-07-14T00:00:00+00:00"),
        ("schema_version", "1"),
    ])
    text = ["product_name", "brands", "image_url", "ingredients_text",
            "quantity", "serving_size", "categories", "allergens", "labels"]
    columns = ["gtin14", "modified", *text, *FIELDS]
    placeholders = ",".join("?" * len(columns))
    db.executemany(
        f"INSERT INTO products ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row.get(c) for c in columns) for row in ROWS],
    )
    db.commit()
    db.close()
    return path


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    path = build_db(tmp_path / "off.sqlite3")
    monkeypatch.setattr(off_local, "DB_PATH", path)
    monkeypatch.setattr(off_local, "_db", None)
    monkeypatch.setattr(off_local, "_metadata", {})
    return path


# ── Lookups ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_known_barcode_is_found(local_db):
    record = await off_local.get_by_gtin("04963406021372")

    assert record["product_name"] == "Coca-Cola Classic"
    assert record["brands"] == "Coca-Cola"
    assert record["barcode"] == "04963406021372"


@pytest.mark.asyncio
async def test_an_unknown_barcode_returns_none_not_an_error(local_db):
    assert await off_local.get_by_gtin("00000000000000") is None


@pytest.mark.asyncio
async def test_a_barcode_is_found_at_any_zero_padding(local_db):
    record = await off_local.get_by_gtin("4963406021372")   # unpadded

    assert record is not None
    assert record["product_name"] == "Coca-Cola Classic"


@pytest.mark.asyncio
async def test_junk_input_is_rejected(local_db):
    assert await off_local.get_by_gtin("not-a-barcode") is None
    assert await off_local.get_by_gtin("") is None


# ── The record is shaped like a live one ──────────────────────────────

@pytest.mark.asyncio
async def test_tag_columns_come_back_as_lists(local_db):
    """The live SDK returns categories/allergens/labels as lists; so must we."""
    record = await off_local.get_by_gtin("04963406021372")

    assert record["categories"] == ["en:beverages", "en:carbonated-drinks"]
    assert record["labels"] == ["en:no-lactose"]
    assert record["allergens"] == []           # empty string -> empty list


@pytest.mark.asyncio
async def test_nutrients_are_stored_raw_and_converted_only_at_lookup(local_db):
    """The whole reason values are stored raw: from_off runs once, here.

    Sodium is stored as 0.004 (4 mg in OFF's grams). from_off multiplies by
    1000. If the build had pre-converted, this lookup would double it.
    """
    record = await off_local.get_by_gtin("04963406021372")
    values = from_off(record["nutrients_per_100g"])

    assert values["calories_kcal"] == 44.0
    assert values["sugars"] == 10.6
    assert values["sodium"] == 4.0             # 0.004 g -> 4 mg, once


@pytest.mark.asyncio
async def test_the_orchestrator_serves_a_local_record(local_db, gpc_db):
    """End to end through the merge: a local hit populates the product.

    Uses the gpc_db fixture so the category lookup runs against a fixture
    database it will *close* — the real one is opened via an aiosqlite
    connection whose non-daemon thread would otherwise hang the interpreter.
    """
    product = await orchestrator.lookup("04963406021372")

    assert "OpenFoodFacts" in product.data_sources
    assert product.product_name == "Coca-Cola Classic"
    assert product.calories_kcal == 44.0
    assert product.sodium.value == 4.0        # 0.004 g raw -> 4 mg via from_off


@pytest.mark.asyncio
async def test_absent_nutrients_are_omitted_not_zeroed(local_db):
    record = await off_local.get_by_gtin("00072940755050")

    assert "protein" in record["nutrients_per_100g"]
    assert "sodium" not in record["nutrients_per_100g"]   # NULL in the row


# ── Degradation ───────────────────────────────────────────────────────

def test_no_local_database_means_no_local_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(off_local, "DB_PATH", tmp_path / "nothing.sqlite3")

    assert off_local.is_available() is False
    assert off_local.stats()["status"] == "absent"


@pytest.mark.asyncio
async def test_a_corrupt_database_degrades_instead_of_failing(tmp_path, monkeypatch):
    broken = tmp_path / "off.sqlite3"
    broken.write_bytes(b"this is not a database")
    monkeypatch.setattr(off_local, "DB_PATH", broken)
    monkeypatch.setattr(off_local, "_db", None)

    assert await off_local.get_by_gtin("04963406021372") is None


@pytest.mark.asyncio
async def test_the_local_tier_can_be_switched_off(local_db, monkeypatch):
    monkeypatch.setattr(off_local, "ENABLED", False)

    assert off_local.is_available() is False
    assert await off_local.get_by_gtin("04963406021372") is None


# ── The archive ───────────────────────────────────────────────────────

def test_the_archive_is_expanded_on_startup(tmp_path, monkeypatch):
    source = build_db(tmp_path / "source.sqlite3")
    archive = tmp_path / "off.sqlite3.xz"
    with open(source, "rb") as src, lzma.open(archive, "wb") as dst:
        dst.write(src.read())
    target = tmp_path / "off.sqlite3"
    monkeypatch.setattr(off_local, "DB_PATH", target)
    monkeypatch.setattr(off_local, "ARCHIVE_PATH", archive)

    assert off_local.ensure_database() is True
    assert target.exists()
    assert sqlite3.connect(target).execute(
        "SELECT COUNT(*) FROM products").fetchone()[0] == len(ROWS)


def test_a_corrupt_archive_leaves_no_half_written_database(tmp_path, monkeypatch):
    archive = tmp_path / "off.sqlite3.xz"
    archive.write_bytes(b"\xfd7zXZ\x00truncated garbage")
    target = tmp_path / "off.sqlite3"
    monkeypatch.setattr(off_local, "DB_PATH", target)
    monkeypatch.setattr(off_local, "ARCHIVE_PATH", archive)

    assert off_local.ensure_database() is False
    assert not target.exists()
    assert not target.with_suffix(".expanding").exists()


def test_health_names_the_dataset_before_any_lookup(local_db):
    reported = off_local.stats()

    assert reported["status"] == "ok"
    assert reported["dataset"] == "off-2026-07-14"
    assert reported["products"] == len(ROWS)


# ── Wiring: local first, API for the rest ─────────────────────────────

@pytest.mark.asyncio
async def test_a_local_hit_never_calls_the_api(local_db, monkeypatch):
    """The reason the copy exists: no network read, no rate-limit token."""
    async def explode(barcode, *a, **k):
        raise AssertionError("the OFF API must not be called for a local hit")

    monkeypatch.setattr(orchestrator.off, "get_product", explode)

    data, _, _ = await orchestrator._fetch_off("04963406021372")

    assert data["product_name"] == "Coca-Cola Classic"


@pytest.mark.asyncio
async def test_a_local_miss_falls_through_to_the_api(local_db, monkeypatch):
    async def upstream(barcode, *a, **k):
        return {"product_name": "BRAND NEW PRODUCT", "code": barcode,
                "nutrients_per_100g": {}}

    monkeypatch.setattr(orchestrator.off, "get_product", upstream)

    data, _, _ = await orchestrator._fetch_off("00000000000000")

    assert data["product_name"] == "BRAND NEW PRODUCT"


@pytest.mark.asyncio
async def test_a_broken_local_copy_still_lets_the_api_answer(tmp_path, monkeypatch):
    broken = tmp_path / "off.sqlite3"
    broken.write_bytes(b"not a database")
    monkeypatch.setattr(off_local, "DB_PATH", broken)
    monkeypatch.setattr(off_local, "_db", None)

    async def upstream(barcode, *a, **k):
        return {"product_name": "FROM THE API", "code": barcode,
                "nutrients_per_100g": {}}

    monkeypatch.setattr(orchestrator.off, "get_product", upstream)

    data, _, _ = await orchestrator._fetch_off("04963406021372")

    assert data["product_name"] == "FROM THE API"


# ── Provenance and the fresh (skip-cache) flag ────────────────────────

def test_provenance_reports_local_with_the_dataset_date(local_db):
    prov = off_local.provenance()

    assert prov["origin"] == "local"
    assert prov["dataset"] == "off-2026-07-14"
    assert prov["dataset_date"] == "2026-07-14"


@pytest.mark.asyncio
async def test_fresh_bypasses_local_off_and_is_tagged_live(local_db, monkeypatch):
    async def upstream(barcode, use_store=True):
        return {"product_name": "LIVE", "code": barcode, "nutrients_per_100g": {}}

    monkeypatch.setattr(orchestrator.off, "get_product", upstream)

    data, _, prov = await orchestrator._fetch_off("04963406021372", fresh=True)

    assert data["product_name"] == "LIVE"
    assert prov == {"origin": "live"}
