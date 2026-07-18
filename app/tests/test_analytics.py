"""
Tests for the Data Quality Dashboard's aggregation layer (PLAN.md item 6).

nutrient_field_coverage() goes through data_browser.coverage(), which has its
own independent store registry (data_browser._STORES, pointing at DATA_DIR) --
separate from fdc_local.DB_PATH/off_local.DB_PATH, which source_summary()
reads directly. The two fixtures below patch the two paths a full summary()
call actually depends on.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core import analytics
from app.core import data_browser as db
from app.core import fdc_local
from app.core import off_local
from app.main import app

client = TestClient(app)


@pytest.fixture
def fdc_store(tmp_path, monkeypatch):
    """A tiny FDC mirror with two real nutrient-named columns, registered
    both as a data_browser store (what nutrient_field_coverage() reads) and
    as fdc_local.DB_PATH (what source_summary() and cross_source_agreement()
    read). Keyed by gtin14 -- the real schema's primary key, and what
    cross_source_agreement()'s JOIN needs -- with gtins 1-3 chosen to
    overlap with off_store's 1-4 below.
    """
    path = tmp_path / "fdc.sqlite3"
    conn = sqlite3.connect(path)
    # `category` is included even though most of these tests don't exercise
    # GPC matching directly -- gpc_match.fdc_category_counts() (called
    # through gpc_matching_summary()) expects the real schema's column.
    conn.execute("CREATE TABLE foods (gtin14 TEXT PRIMARY KEY, category TEXT, "
                 "calories_kcal REAL, protein REAL)")
    conn.executemany("INSERT INTO foods VALUES (?,?,?,?)", [
        ("1", "Snack", 100.0, 5.0), ("2", "Snack", 200.0, None), ("3", "Snack", None, None),
    ])
    conn.execute("CREATE TABLE fdc_metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO fdc_metadata VALUES (?,?)", [
        ("dataset", "FoodData_Central_branded_food_csv_2026-04-30"),
        ("barcodes", "3"),
    ])
    conn.commit()
    conn.close()

    s = db.SqliteStore("fdc", "USDA FDC", "fixture", "fdc.sqlite3")
    s.path = path
    monkeypatch.setattr(db, "_cache", {})
    monkeypatch.setattr(fdc_local, "DB_PATH", path)
    return s


@pytest.fixture
def off_store(tmp_path, monkeypatch):
    """A tiny OFF mirror with one overlapping and one OFF-only nutrient
    column, same dual registration as fdc_store. gtin "1" and "2" agree
    with FDC's calories_kcal within tolerance; gtin "3" has no FDC
    calories_kcal to compare against (matched-pair count is 2, not 3);
    gtin "4" doesn't exist in FDC at all (not a matched GTIN)."""
    path = tmp_path / "off.sqlite3"
    conn = sqlite3.connect(path)
    # `categories` is included for the same reason `foods.category` is in
    # fdc_store -- gpc_match.off_tag_counts() expects the real column name.
    conn.execute("CREATE TABLE products (gtin14 TEXT PRIMARY KEY, categories TEXT, "
                 "calories_kcal REAL, sodium REAL)")
    conn.executemany("INSERT INTO products VALUES (?,?,?,?)", [
        ("1", "en:snacks", 110.0, 0.4), ("2", "en:snacks", 220.0, 0.5),
        ("3", "en:snacks", 330.0, None), ("4", "en:snacks", None, None),
    ])
    conn.execute("CREATE TABLE off_metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO off_metadata VALUES (?,?)", [
        ("dataset", "off-2026-07-17"), ("products", "4"),
    ])
    conn.commit()
    conn.close()

    s = db.SqliteStore("off", "Open Food Facts", "fixture", "off.sqlite3")
    s.path = path
    monkeypatch.setattr(off_local, "DB_PATH", path)
    return s


@pytest.fixture
def both_stores(fdc_store, off_store, monkeypatch):
    monkeypatch.setattr(db, "_STORES", {"fdc": fdc_store, "off": off_store})
    monkeypatch.setattr(db, "_cache", {})
    monkeypatch.setattr(analytics, "_cross_source_cache", {})
    return fdc_store, off_store


# ── nutrient_field_coverage ──────────────────────────────────────────────

def test_returns_an_entry_for_every_nutrient_field(both_stores):
    rows = analytics.nutrient_field_coverage()
    from app.core.nutrients import NUTRIENTS
    assert {r["field"] for r in rows} == {spec.field for spec in NUTRIENTS}


def test_reports_correct_percentages_for_present_columns(both_stores):
    rows = {r["field"]: r for r in analytics.nutrient_field_coverage()}
    # calories_kcal: FDC 2/3 non-null, OFF 3/4 non-null.
    assert rows["calories_kcal"]["fdc_pct"] == pytest.approx(66.7, abs=.1)
    assert rows["calories_kcal"]["off_pct"] == 75.0
    # protein exists only in the FDC fixture; sodium only in the OFF one.
    assert rows["protein"]["fdc_pct"] == pytest.approx(33.3, abs=.1)
    assert rows["protein"]["off_pct"] is None
    assert rows["sodium"]["fdc_pct"] is None
    assert rows["sodium"]["off_pct"] == 50.0


def test_a_field_in_neither_mirror_reports_both_none(both_stores):
    rows = {r["field"]: r for r in analytics.nutrient_field_coverage()}
    assert rows["biotin"]["fdc_pct"] is None
    assert rows["biotin"]["off_pct"] is None


def test_degrades_gracefully_when_a_source_is_entirely_absent(fdc_store, monkeypatch):
    """OFF has no local mirror at all -- FDC's numbers must still come
    through, not the whole function failing."""
    monkeypatch.setattr(db, "_STORES", {"fdc": fdc_store})
    rows = {r["field"]: r for r in analytics.nutrient_field_coverage()}
    assert rows["calories_kcal"]["fdc_pct"] == pytest.approx(66.7, abs=.1)
    assert rows["calories_kcal"]["off_pct"] is None


# ── source_summary / gpc_matching_summary / summary ─────────────────────

async def test_source_summary_reports_each_mirrors_dataset(both_stores):
    result = await analytics.source_summary()
    assert result["fdc"]["dataset"] == "FoodData_Central_branded_food_csv_2026-04-30"
    assert result["fdc"]["barcodes"] == 3
    assert result["off"]["dataset"] == "off-2026-07-17"
    assert result["off"]["products"] == 4


async def test_source_summary_gpc_degrades_when_the_gpc_db_is_unavailable():
    """isolated_gpc_db (autouse, conftest.py) points at a nonexistent path by
    default -- the gpc portion of source_summary() must degrade, not raise."""
    result = await analytics.source_summary()
    assert result["gpc"]["status"] == "error"


async def test_source_summary_gpc_reports_counts(gpc_db):
    result = await analytics.source_summary()
    assert result["gpc"]["status"] == "ok"
    assert result["gpc"]["bricks"] == 3       # gpc_db fixture: Cola Drinks, Lemonade, Apples
    assert result["gpc"]["version"] == "test"


def test_gpc_matching_summary_reports_both_reports_side_by_side(monkeypatch):
    from app.core import gpc_match
    monkeypatch.setattr(gpc_match, "fdc_category_counts", lambda: None)
    monkeypatch.setattr(gpc_match, "off_tag_counts", lambda: None)
    result = analytics.gpc_matching_summary()
    assert result["fdc_curated"] is None
    assert result["reviewed"] is None


# ── cross_source_agreement ────────────────────────────────────────────

def test_cross_source_is_none_when_a_mirror_is_missing(fdc_store, monkeypatch):
    monkeypatch.setattr(off_local, "DB_PATH", fdc_store.path.parent / "absent_off.sqlite3")
    assert analytics.cross_source_agreement() is None


def test_cross_source_reports_the_tolerance(both_stores):
    result = analytics.cross_source_agreement()
    assert result["tolerance"] == analytics._AGREEMENT_TOLERANCE


def test_cross_source_matched_pairs_require_both_sides_non_null(both_stores):
    fields = {f["field"]: f for f in analytics.cross_source_agreement()["fields"]}
    # gtin 1 and 2 have calories_kcal on both sides; gtin 3 is FDC-null,
    # gtin 4 doesn't exist in FDC at all -- so matched_gtins is 2, not 4.
    assert fields["calories_kcal"]["matched_gtins"] == 2


def test_cross_source_agreement_within_tolerance(both_stores):
    fields = {f["field"]: f for f in analytics.cross_source_agreement()["fields"]}
    # gtin1: FDC 100 vs OFF 110 (10 <= 15% of 110); gtin2: 200 vs 220 (20 <=
    # 15% of 220) -- both within tolerance.
    assert fields["calories_kcal"]["agree"] == 2
    assert fields["calories_kcal"]["agree_pct"] == 100.0


def test_cross_source_a_field_present_in_only_one_mirror_has_no_matched_pairs(both_stores):
    fields = {f["field"]: f for f in analytics.cross_source_agreement()["fields"]}
    assert fields["protein"]["matched_gtins"] == 0    # OFF fixture has no protein column
    assert fields["protein"]["agree_pct"] is None
    assert fields["sodium"]["matched_gtins"] == 0     # FDC fixture has no sodium column


def test_cross_source_disagreement_is_counted_separately_from_agreement(
        fdc_store, off_store, monkeypatch):
    """A pair genuinely outside tolerance must lower agree_pct, not be
    silently folded into the agreeing count."""
    conn = sqlite3.connect(fdc_store.path)
    conn.execute("UPDATE foods SET calories_kcal = 1000.0 WHERE gtin14 = '1'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "_STORES", {"fdc": fdc_store, "off": off_store})
    monkeypatch.setattr(analytics, "_cross_source_cache", {})

    fields = {f["field"]: f for f in analytics.cross_source_agreement()["fields"]}
    assert fields["calories_kcal"]["matched_gtins"] == 2
    assert fields["calories_kcal"]["agree"] == 1     # gtin2 still agrees; gtin1 no longer does
    assert fields["calories_kcal"]["agree_pct"] == 50.0


def test_cross_source_scales_off_grams_to_published_unit_before_comparing(
        fdc_store, off_store, monkeypatch):
    """sodium is in nutrients._OFF_GRAMS_TO_MG -- off_store stores it raw
    (0.4g for gtin 1), fdc_store publishes it in mg. Comparing the two
    columns without scaling would read as 0.4 vs 390, a ~10,000% miss; the
    real figures (400mg vs 390mg) are within tolerance."""
    conn = sqlite3.connect(fdc_store.path)
    conn.execute("ALTER TABLE foods ADD COLUMN sodium REAL")
    conn.execute("UPDATE foods SET sodium = 390.0 WHERE gtin14 = '1'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "_STORES", {"fdc": fdc_store, "off": off_store})
    monkeypatch.setattr(analytics, "_cross_source_cache", {})

    fields = {f["field"]: f for f in analytics.cross_source_agreement()["fields"]}
    assert fields["sodium"]["matched_gtins"] == 1
    assert fields["sodium"]["agree"] == 1
    assert fields["sodium"]["agree_pct"] == 100.0


async def test_summary_combines_all_four_sections(both_stores, gpc_db):
    result = await analytics.summary()
    assert set(result) == {
        "sources", "nutrient_coverage", "gpc_matching", "cross_source_agreement"}
    assert result["sources"]["fdc"]["dataset"]
    assert len(result["nutrient_coverage"]) > 0
    assert "fdc_curated" in result["gpc_matching"]
    assert result["cross_source_agreement"]["tolerance"] == analytics._AGREEMENT_TOLERANCE


# ── GET /api/v1/data/analytics route ─────────────────────────────────────

def test_analytics_route_returns_the_full_payload(both_stores, gpc_db):
    body = client.get("/api/v1/data/analytics").json()
    assert set(body) == {
        "sources", "nutrient_coverage", "gpc_matching", "cross_source_agreement"}
    assert body["sources"]["off"]["dataset"] == "off-2026-07-17"
