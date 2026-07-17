"""
Tests for the GPC mapping viewer: bulk hierarchy lookups against the curated
tables, live coverage measurement against the local FDC bulk copy, and the
/api/v1/gpc/mappings route that ties them together.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core import fdc_local
from app.core import gpc_match
from app.main import app

client = TestClient(app)


@pytest.fixture
def fdc_db(tmp_path, monkeypatch):
    """A tiny local FDC bulk copy, with just the `category` column this
    module reads."""
    path = tmp_path / "fdc_fixture.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE foods (fdc_id INTEGER PRIMARY KEY, category TEXT)")
    conn.executemany("INSERT INTO foods (category) VALUES (?)", [
        ("Bread",), ("Bread",), ("Bread",),
        ("Cheese",), ("Cheese",),
        ("Mystery Snacks",),   # not in either curated table
        (None,),               # no category at all -- excluded from the denominator
        ("",),                 # blank category -- also excluded
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(fdc_local, "DB_PATH", path)
    return path


# ── fdc_category_counts / coverage_report ───────────────────────────────

def test_category_counts_none_without_a_local_fdc_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(fdc_local, "DB_PATH", tmp_path / "absent.sqlite3")
    assert gpc_match.fdc_category_counts() is None
    assert gpc_match.coverage_report() is None


def test_category_counts_excludes_null_and_blank(fdc_db):
    assert gpc_match.fdc_category_counts() == {"Bread": 3, "Cheese": 2, "Mystery Snacks": 1}


def test_coverage_report_totals(fdc_db, monkeypatch):
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Bread": "10000165"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {"Cheese": "50131800"})

    report = gpc_match.coverage_report()

    assert report["total_categorized_foods"] == 6
    assert report["covered_foods"] == 5  # Bread(3) + Cheese(2)
    assert report["coverage_pct"] == pytest.approx(83.3, abs=0.1)
    assert report["distinct_fdc_categories"] == 3
    assert report["curated_brick_entries"] == 1
    assert report["curated_class_entries"] == 1
    assert report["uncovered_categories"] == [{"category": "Mystery Snacks", "food_count": 1}]


def test_coverage_report_ranks_uncovered_by_size(fdc_db, monkeypatch):
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {})

    report = gpc_match.coverage_report()

    assert [u["category"] for u in report["uncovered_categories"]] == [
        "Bread", "Cheese", "Mystery Snacks"]
    assert report["covered_foods"] == 0
    assert report["coverage_pct"] == 0.0


def test_coverage_report_matches_the_real_lookup_for_whitespace_padded_categories(
    tmp_path, monkeypatch,
):
    """FDC has at least one real category with a literal trailing space
    ("Cheese - Speciality "). coverage_report() counts a raw category as
    covered by checking dict membership; curated_brick_for_fdc_category()
    resolves it at request time by stripping first. If those two disagree,
    the coverage number on /gpc/mappings lies about what the orchestrator
    actually resolves -- this pins them together."""
    path = tmp_path / "fdc_whitespace.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE foods (fdc_id INTEGER PRIMARY KEY, category TEXT)")
    conn.executemany("INSERT INTO foods (category) VALUES (?)", [
        ("Cheese - Speciality ",),  # trailing space, exactly like the real FDC data
        ("Cheese - Speciality ",),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(fdc_local, "DB_PATH", path)
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Cheese - Speciality": "10000028"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {})

    report = gpc_match.coverage_report()

    assert report["covered_foods"] == 2
    assert report["uncovered_categories"] == []
    # And the real runtime path resolves the same raw (unstripped) value too.
    assert gpc_match.curated_brick_for_fdc_category("Cheese - Speciality ") == "10000028"


# ── hierarchy_for_bricks / hierarchy_for_classes (bulk) ──────────────────

async def test_hierarchy_for_bricks_batches_known_and_ignores_unknown(gpc_db):
    from app.database import get_db
    db = await get_db()
    result = await gpc_match.hierarchy_for_bricks(db, ["10000201", "10000202", "99999999"])
    assert set(result) == {"10000201", "10000202"}
    assert result["10000201"] == ["Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks"]
    assert result["10000202"] == ["Food/Beverage", "Beverages", "Carbonated Drinks", "Lemonade"]


async def test_hierarchy_for_bricks_of_no_codes_is_empty(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await gpc_match.hierarchy_for_bricks(db, []) == {}


async def test_hierarchy_for_classes_batches_known_and_ignores_unknown(gpc_db):
    from app.database import get_db
    db = await get_db()
    result = await gpc_match.hierarchy_for_classes(db, ["50202300", "50101800", "99999999"])
    assert set(result) == {"50202300", "50101800"}
    assert result["50202300"] == ["Food/Beverage", "Beverages", "Carbonated Drinks"]
    assert result["50101800"] == ["Food/Beverage", "Fruits/Vegetables", "Fresh Fruits"]


async def test_hierarchy_for_classes_of_no_codes_is_empty(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await gpc_match.hierarchy_for_classes(db, []) == {}


# ── /api/v1/gpc/mappings route ────────────────────────────────────────

def test_mappings_route_returns_resolved_hierarchy_and_coverage(gpc_db, fdc_db, monkeypatch):
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Bread": "10000201"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {"Cheese": "50101800"})

    body = client.get("/api/v1/gpc/mappings").json()

    assert len(body["mappings"]) == 2
    brick_entry = next(m for m in body["mappings"] if m["level"] == "brick")
    assert brick_entry == {
        "category": "Bread", "level": "brick", "code": "10000201",
        "hierarchy": ["Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks"],
    }
    class_entry = next(m for m in body["mappings"] if m["level"] == "class")
    assert class_entry == {
        "category": "Cheese", "level": "class", "code": "50101800",
        "hierarchy": ["Food/Beverage", "Fruits/Vegetables", "Fresh Fruits"],
    }

    coverage = body["coverage"]
    assert coverage["total_categorized_foods"] == 6
    assert coverage["covered_foods"] == 5
    assert coverage["curated_brick_entries"] == 1
    assert coverage["curated_class_entries"] == 1


def test_mappings_route_coverage_is_null_without_a_local_fdc_copy(gpc_db, monkeypatch):
    """isolated_fdc_local (autouse) already points at a nonexistent copy --
    the route must degrade to a null coverage block, not error."""
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Bread": "10000201"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {})

    body = client.get("/api/v1/gpc/mappings").json()

    assert body["coverage"] is None
    assert len(body["mappings"]) == 1


def test_mappings_route_unresolved_code_gets_an_empty_hierarchy(gpc_db, monkeypatch):
    """A curated entry pointing at a code that isn't in the (fixture) GPC
    database must still appear in the list -- with an empty hierarchy, not
    a crash or a silently dropped row."""
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Ghost": "99999999"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {})

    body = client.get("/api/v1/gpc/mappings").json()

    assert body["mappings"] == [
        {"category": "Ghost", "level": "brick", "code": "99999999", "hierarchy": []}]
