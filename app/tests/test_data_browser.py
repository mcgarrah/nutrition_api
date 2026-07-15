"""
Tests for the read-only Data Browser.

The browser lets a caller introspect and page through the local SQLite stores
and the file-based response cache. The load-bearing property is safety: a table
or column name that isn't real must never reach SQL, so the browser can't be
turned into an arbitrary-query or write tool. These tests build tiny fixture
stores (never the real multi-GB databases) and exercise browsing, coverage, and
the injection guards.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core import data_browser as db
from app.core import store

client = TestClient(app)


@pytest.fixture
def fixture_store(tmp_path, monkeypatch):
    """A single small SQLite store registered as 'demo'."""
    path = tmp_path / "demo.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, "
                 "note TEXT, qty REAL)")
    conn.executemany("INSERT INTO items VALUES (?,?,?,?)", [
        (1, "Olive Oil", "shelf stable", 100.0),
        (2, "Olive Oil", None, None),          # a NULL note and qty
        (3, "Parmesan", "aged", 40.0),
        (4, "Water", None, 0.0),
    ])
    conn.commit()
    conn.close()

    s = db.SqliteStore("demo", "Demo", "a fixture", "demo.sqlite3")
    s.path = path
    monkeypatch.setattr(db, "_STORES", {"demo": s})
    monkeypatch.setattr(db, "_cache", {})
    return s


# ── Listing and schema ────────────────────────────────────────────────

def test_list_stores_reports_tables_and_size(fixture_store):
    stores = db.list_stores()
    assert len(stores) == 1
    demo = stores[0]
    assert demo["id"] == "demo" and demo["available"] is True
    assert demo["kind"] == "sqlite"
    assert {t["name"]: t["rows"] for t in demo["tables"]} == {"items": 4}


def test_schema_exposes_columns_and_the_primary_key(fixture_store):
    tables = db.schema("demo")
    items = next(t for t in tables if t["name"] == "items")
    names = [c["name"] for c in items["columns"]]
    assert names == ["id", "name", "note", "qty"]
    assert next(c for c in items["columns"] if c["name"] == "id")["pk"] is True


def test_schema_of_an_unknown_store_is_none(fixture_store):
    assert db.schema("nope") is None


# ── Browsing rows ─────────────────────────────────────────────────────

def test_rows_paginate_and_report_the_total(fixture_store):
    page = db.rows("demo", "items", limit=2, offset=0)
    assert page["total"] == 4
    assert len(page["rows"]) == 2
    assert page["columns"] == ["id", "name", "note", "qty"]


def test_rows_search_scans_the_text_columns(fixture_store):
    page = db.rows("demo", "items", q="olive")
    assert page["matched"] == 2
    assert all("Olive" in r[1] for r in page["rows"])


def test_rows_sort_by_a_real_column(fixture_store):
    page = db.rows("demo", "items", sort="name", direction="desc")
    assert page["rows"][0][1] == "Water"          # Z→A


def test_a_null_value_survives_as_none(fixture_store):
    page = db.rows("demo", "items", q="Water")
    assert page["rows"][0][2] is None             # the NULL note


# ── Injection / safety ────────────────────────────────────────────────

def test_an_unknown_table_returns_none_never_runs_sql(fixture_store):
    assert db.rows("demo", "items; DROP TABLE items", limit=1) is None
    assert db.coverage("demo", "definitely not a table") is None
    # the real table is untouched
    assert db.rows("demo", "items")["total"] == 4


def test_a_bogus_sort_column_is_ignored_not_injected(fixture_store):
    # A sort that isn't a real column falls back to the primary key.
    page = db.rows("demo", "items", sort="qty); DROP TABLE items;--")
    assert [r[0] for r in page["rows"]] == [1, 2, 3, 4]   # PK order, intact


# ── Coverage ──────────────────────────────────────────────────────────

def test_coverage_reports_non_null_percentages(fixture_store):
    cov = db.coverage("demo", "items")
    pct = {c["name"]: c["pct"] for c in cov["columns"]}
    assert cov["total"] == 4
    assert pct["name"] == 100.0
    assert pct["note"] == 50.0                    # 2 of 4 non-null


# ── The response store (file adapter) ─────────────────────────────────

def test_the_response_store_is_browsable_as_namespaces(monkeypatch):
    # conftest's isolated_response_store already points the store at a temp dir.
    monkeypatch.setattr(db, "_STORES", dict(store=db._STORES["store"]))
    store.put(store.OFF_PRODUCT, "12345", {"product_name": "Cola"})
    store.put(store.OFF_PRODUCT, "67890", {"product_name": "Water"})

    page = db.rows("store", store.OFF_PRODUCT)
    assert page["total"] == 2
    keys = [r[0] for r in page["rows"]]
    assert "12345" in keys and "67890" in keys

    rec = db.record("store", store.OFF_PRODUCT, "12345")
    assert rec["payload"] == {"product_name": "Cola"}


# ── Route layer ───────────────────────────────────────────────────────

def test_rows_route_404s_on_an_unknown_table(fixture_store):
    r = client.get("/api/v1/data/demo/rows", params={"table": "ghost"})
    assert r.status_code == 404


def test_stores_route_lists_the_registry(fixture_store):
    body = client.get("/api/v1/data/stores").json()
    assert [s["id"] for s in body["stores"]] == ["demo"]
