"""
Tests for local-DB-backed product name search: the search.py logic and the
GET /api/v1/search route.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import inspect
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core import fdc_local
from app.core import off_local
from app.core import search
from app.core.search_routes import search_by_name
from app.main import app

client = TestClient(app)


def test_search_route_is_sync_so_fastapi_runs_it_in_the_threadpool():
    """search_products() runs blocking sqlite3 queries with no executor of its
    own; only Starlette's automatic threadpooling for a `def` (not `async
    def`) route keeps a slow scan off the event loop. Regression guard for
    the bug measured at ~17s blocking the whole worker — see PLAN.md item 2."""
    assert not inspect.iscoroutinefunction(search_by_name)


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


def _build_fdc_db_with_fts(path):
    """Same fixture data as _build_fdc_db, plus the foods_fts index a real
    scripts/build_fdc_db.py build now produces (schema_version 2)."""
    _build_fdc_db(path)
    db = sqlite3.connect(path)
    db.execute("""CREATE VIRTUAL TABLE foods_fts USING fts5(
        gtin14 UNINDEXED, description,
        tokenize = 'unicode61 remove_diacritics 2'
    )""")
    db.execute("INSERT INTO foods_fts (gtin14, description) "
               "SELECT gtin14, description FROM foods")
    db.commit()
    db.close()
    return path


def _build_off_db_with_fts(path):
    """Same fixture data as _build_off_db, plus the products_fts index a real
    scripts/build_off_db.py build now produces (schema_version 2)."""
    _build_off_db(path)
    db = sqlite3.connect(path)
    db.execute("""CREATE VIRTUAL TABLE products_fts USING fts5(
        gtin14 UNINDEXED, product_name,
        tokenize = 'unicode61 remove_diacritics 2'
    )""")
    db.execute("INSERT INTO products_fts (gtin14, product_name) "
               "SELECT gtin14, product_name FROM products")
    db.commit()
    db.close()
    return path


@pytest.fixture
def local_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(fdc_local, "DB_PATH", _build_fdc_db(tmp_path / "fdc.sqlite3"))
    monkeypatch.setattr(off_local, "DB_PATH", _build_off_db(tmp_path / "off.sqlite3"))


@pytest.fixture
def local_dbs_fts(tmp_path, monkeypatch):
    """Mirrors built with foods_fts/products_fts present, as a real build
    now produces — exercises the FTS5 query path rather than the LIKE
    fallback that `local_dbs` (no FTS tables) exercises."""
    monkeypatch.setattr(
        fdc_local, "DB_PATH", _build_fdc_db_with_fts(tmp_path / "fdc.sqlite3"))
    monkeypatch.setattr(
        off_local, "DB_PATH", _build_off_db_with_fts(tmp_path / "off.sqlite3"))


# ── search_products (FTS5 path) ─────────────────────────────────────

def test_fts_multi_word_query_matches_regardless_of_word_order(local_dbs_fts):
    """FTS5 ANDs the per-word prefix tokens, not a phrase, so word order in
    the query does not have to match the product name's word order."""
    assert len(search.search_products("bran raisin")) == 1
    assert search.search_products("bran raisin")[0]["gtin"] == "00016000116597"


def test_fts_query_is_a_prefix_match_per_word(local_dbs_fts):
    """"toma" should hit "Tomatoes" via FTS5's trailing "*" prefix operator,
    the same way a LIKE '%toma%' scan would have."""
    results = search.search_products("toma")

    assert len(results) == 1
    assert results[0]["gtin"] == "00072940755050"


def test_fts_search_is_case_insensitive(local_dbs_fts):
    assert len(search.search_products("TOMATOES")) == 1
    assert len(search.search_products("ToMaToEs")) == 1


def test_fts_hyphenated_product_name_is_still_findable(local_dbs_fts):
    """unicode61 tokenizes "Coca-Cola" as two words (coca, cola); searching
    with or without the hyphen must find it either way."""
    assert len(search.search_products("coca-cola")) == 1
    assert len(search.search_products("coca cola")) == 1


def test_fts_a_query_with_no_word_characters_returns_no_results(local_dbs_fts):
    """"!!!" has nothing an FTS5 MATCH expression can be built from -- this
    must degrade to an empty result, not a MATCH syntax error."""
    assert search.search_products("!!!") == []


def test_fts_path_matches_the_like_fallback_on_the_same_fixture_data(
        tmp_path, monkeypatch):
    """The two query paths should agree on ordinary queries -- FTS5 is meant
    to be a faster route to the same answer, not a different one."""
    monkeypatch.setattr(
        fdc_local, "DB_PATH", _build_fdc_db(tmp_path / "like_fdc.sqlite3"))
    monkeypatch.setattr(
        off_local, "DB_PATH", _build_off_db(tmp_path / "like_off.sqlite3"))
    like_gtins = {r["gtin"] for r in search.search_products("raisin bran")}

    monkeypatch.setattr(
        fdc_local, "DB_PATH", _build_fdc_db_with_fts(tmp_path / "fts_fdc.sqlite3"))
    monkeypatch.setattr(
        off_local, "DB_PATH", _build_off_db_with_fts(tmp_path / "fts_off.sqlite3"))
    fts_gtins = {r["gtin"] for r in search.search_products("raisin bran")}

    assert like_gtins == fts_gtins == {"00016000116597"}


def test_no_match_returns_an_empty_list_not_an_error_fts(local_dbs_fts):
    assert search.search_products("nonexistent product xyz") == []


# ── _fts_match_expr ──────────────────────────────────────────────────

def test_fts_match_expr_quotes_and_prefixes_each_word():
    assert search._fts_match_expr("peanut butter") == '"peanut"* "butter"*'


def test_fts_match_expr_lowercases():
    assert search._fts_match_expr("PEANUT") == '"peanut"*'


def test_fts_match_expr_strips_punctuation_between_words():
    assert search._fts_match_expr("coca-cola") == '"coca"* "cola"*'


def test_fts_match_expr_returns_none_for_no_word_characters():
    assert search._fts_match_expr("!!!") is None
    assert search._fts_match_expr("   ") is None


# ── search_products (LIKE fallback path) ────────────────────────────

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


# ── sources= scoping (PLAN.md item 10) ────────────────────────────────

def test_sources_fdc_excludes_a_match_that_only_off_has(local_dbs):
    """"coca-cola" only matches in the OFF fixture -- sources="fdc" must
    return nothing, not silently fall through to OFF."""
    assert search.search_products("coca-cola", sources="fdc") == []
    assert search.search_products("coca-cola", sources="both") != []


def test_sources_off_excludes_a_match_that_only_fdc_has(local_dbs):
    """"raisin bran" only matches in the FDC fixture -- sources="off" must
    return nothing."""
    assert search.search_products("raisin bran", sources="off") == []
    assert search.search_products("raisin bran", sources="both") != []


def test_sources_fdc_still_returns_a_barcode_present_in_both(local_dbs):
    """00072940755050 is in both fixtures; scoped to fdc it must come back
    under FDC's own name/source, not OFF's (which sources="both" prefers)."""
    results = search.search_products("tomatoes", sources="fdc")

    assert len(results) == 1
    assert results[0]["source"] == "USDA_FDC"
    assert results[0]["product_name"] == "Italian Diced Tomatoes"


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


def test_route_honours_the_sources_param(local_dbs):
    body = client.get("/api/v1/search", params={"q": "coca-cola", "sources": "fdc"}).json()
    assert body["results"] == []

    body = client.get("/api/v1/search", params={"q": "coca-cola", "sources": "both"}).json()
    assert len(body["results"]) == 1


def test_route_rejects_an_unknown_sources_value(local_dbs):
    resp = client.get("/api/v1/search", params={"q": "a", "sources": "bogus"})

    assert resp.status_code == 422
