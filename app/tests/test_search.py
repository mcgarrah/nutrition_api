"""
Tests for local-DB-backed product name search: the search.py logic and the
GET /api/v1/search route.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core import fdc_local
from app.core import off_local
from app.core import search
from app.main import app

client = TestClient(app)


def _build_fdc_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE foods (
        gtin14 TEXT PRIMARY KEY, fdc_id INTEGER, description TEXT,
        brand_owner TEXT, brand_name TEXT
    )""")
    db.executemany("INSERT INTO foods VALUES (?,?,?,?,?)", [
        ("00072940755050", 1, "Italian Diced Tomatoes", "Red Gold", None),
        ("00016000116597", 2, "Total Raisin Bran Cereal", "General Mills", None),
        ("00000000000001", 3, "Plain Yogurt", None, "Dairy Co"),
    ])
    db.commit()
    db.close()
    return path


def _build_off_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE products (
        gtin14 TEXT PRIMARY KEY, product_name TEXT, brands TEXT, image_url TEXT
    )""")
    db.executemany("INSERT INTO products VALUES (?,?,?,?)", [
        ("00072940755050", "Diced Tomatoes", "Red Gold",
         "https://images.example.org/tomatoes.jpg"),
        ("04963406021372", "Coca-Cola Classic", "Coca-Cola",
         "https://images.example.org/coke.jpg"),
    ])
    db.commit()
    db.close()
    return path


@pytest.fixture
def local_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(fdc_local, "DB_PATH", _build_fdc_db(tmp_path / "fdc.sqlite3"))
    monkeypatch.setattr(off_local, "DB_PATH", _build_off_db(tmp_path / "off.sqlite3"))


# ── search_products ──────────────────────────────────────────────────

def test_a_match_in_both_copies_is_returned_once(local_dbs):
    """00072940755050 exists in both fixtures under slightly different names."""
    results = search.search_products("tomatoes")

    assert len(results) == 1
    assert results[0]["gtin"] == "00072940755050"


def test_off_wins_a_collision_but_fdc_still_fills_a_gap(local_dbs):
    """OFF's name/brand/image win when both copies have the barcode; a barcode
    found only in FDC is still returned, not dropped."""
    results = search.search_products("tomatoes")

    assert results[0]["product_name"] == "Diced Tomatoes"   # OFF's name, not FDC's
    assert results[0]["image_url"] == "https://images.example.org/tomatoes.jpg"
    assert results[0]["source"] == "OpenFoodFacts"


def test_a_match_only_in_fdc_is_still_returned(local_dbs):
    results = search.search_products("raisin bran")

    assert len(results) == 1
    assert results[0]["gtin"] == "00016000116597"
    assert results[0]["source"] == "USDA_FDC"
    assert results[0]["image_url"] is None


def test_a_match_only_in_off_is_still_returned(local_dbs):
    results = search.search_products("coca-cola")

    assert len(results) == 1
    assert results[0]["source"] == "OpenFoodFacts"


def test_no_match_returns_an_empty_list_not_an_error(local_dbs):
    assert search.search_products("nonexistent product xyz") == []


def test_blank_query_returns_no_results_without_touching_the_databases(local_dbs):
    """An empty search would otherwise LIKE-match every row in both copies."""
    assert search.search_products("") == []
    assert search.search_products("   ") == []


def test_search_is_case_insensitive(local_dbs):
    assert len(search.search_products("TOMATOES")) == 1
    assert len(search.search_products("ToMaToEs")) == 1


def test_limit_is_respected_and_capped(local_dbs):
    assert len(search.search_products("tomatoes", limit=0)) <= 1  # clamped to >= 1
    # A limit above MAX_RESULTS is clamped, not honoured verbatim.
    results = search.search_products("tomatoes", limit=9999)
    assert len(results) <= search.MAX_RESULTS


def test_a_brandless_fdc_row_falls_back_to_brand_name(local_dbs):
    results = search.search_products("plain yogurt")

    assert results[0]["brand"] == "Dairy Co"   # brand_owner was None


def test_missing_local_copies_degrade_to_no_results(tmp_path, monkeypatch):
    monkeypatch.setattr(fdc_local, "DB_PATH", tmp_path / "absent_fdc.sqlite3")
    monkeypatch.setattr(off_local, "DB_PATH", tmp_path / "absent_off.sqlite3")

    assert search.search_products("tomatoes") == []


# ── GET /api/v1/search ───────────────────────────────────────────────

def test_route_returns_the_query_and_results(local_dbs):
    body = client.get("/api/v1/search", params={"q": "tomatoes"}).json()

    assert body["query"] == "tomatoes"
    assert len(body["results"]) == 1
    assert body["results"][0]["gtin"] == "00072940755050"


def test_route_with_no_query_returns_empty_results(local_dbs):
    body = client.get("/api/v1/search").json()

    assert body["results"] == []


def test_route_honours_the_limit_param(local_dbs):
    body = client.get("/api/v1/search", params={"q": "a", "limit": 1}).json()

    assert len(body["results"]) <= 1


def test_route_rejects_a_limit_above_the_max(local_dbs):
    resp = client.get("/api/v1/search", params={"q": "a", "limit": search.MAX_RESULTS + 1})

    assert resp.status_code == 422
