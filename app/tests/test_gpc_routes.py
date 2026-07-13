"""
Tests for the GPC browser routes, backed by a small fixture SQLite database.

The fixture database mirrors the production schema (including the junction
tables for brick attributes) with a handful of rows, so these tests exercise
the real SQL without needing the full GS1 import.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.main import app

client = TestClient(app)

_FIXTURE_SCHEMA = """
CREATE TABLE segments (segment_code TEXT PRIMARY KEY, description TEXT);
CREATE TABLE families (
    family_code TEXT PRIMARY KEY, description TEXT,
    segment_code TEXT NOT NULL REFERENCES segments(segment_code)
);
CREATE TABLE classes (
    class_code TEXT PRIMARY KEY, description TEXT,
    family_code TEXT NOT NULL REFERENCES families(family_code)
);
CREATE TABLE bricks (
    brick_code TEXT PRIMARY KEY, description TEXT,
    class_code TEXT NOT NULL REFERENCES classes(class_code)
);
CREATE TABLE attribute_types (att_type_code TEXT PRIMARY KEY, att_type_text TEXT);
CREATE TABLE attribute_values (att_value_code TEXT PRIMARY KEY, att_value_text TEXT);
CREATE TABLE brick_attribute_types (
    brick_code TEXT NOT NULL, att_type_code TEXT NOT NULL,
    PRIMARY KEY (brick_code, att_type_code)
);
CREATE TABLE attribute_type_values (
    att_type_code TEXT NOT NULL, att_value_code TEXT NOT NULL,
    PRIMARY KEY (att_type_code, att_value_code)
);
CREATE TABLE gpc_metadata (key TEXT PRIMARY KEY, value TEXT);
"""

_FIXTURE_ROWS = [
    ("INSERT INTO segments VALUES (?, ?)", [
        ("50000000", "Food/Beverage"),
        ("51000000", "Healthcare"),
    ]),
    ("INSERT INTO families VALUES (?, ?, ?)", [
        ("50200000", "Beverages", "50000000"),
        ("50100000", "Fruits/Vegetables", "50000000"),
    ]),
    ("INSERT INTO classes VALUES (?, ?, ?)", [
        ("50202300", "Carbonated Drinks", "50200000"),
        ("50101800", "Fresh Fruits", "50100000"),
    ]),
    ("INSERT INTO bricks VALUES (?, ?, ?)", [
        ("10000201", "Cola Drinks", "50202300"),
        ("10000202", "Lemonade", "50202300"),
        ("10005900", "Apples", "50101800"),
    ]),
    ("INSERT INTO attribute_types VALUES (?, ?)", [
        ("20000100", "Caffeine Presence"),
    ]),
    ("INSERT INTO attribute_values VALUES (?, ?)", [
        ("30000101", "Caffeinated"),
        ("30000102", "Decaffeinated"),
    ]),
    ("INSERT INTO brick_attribute_types VALUES (?, ?)", [
        ("10000201", "20000100"),
    ]),
    ("INSERT INTO attribute_type_values VALUES (?, ?)", [
        ("20000100", "30000101"),
        ("20000100", "30000102"),
    ]),
    ("INSERT INTO gpc_metadata VALUES (?, ?)", [
        ("gpc_version", "test"),
        ("xml_date", "2026-01-01"),
        ("import_timestamp", "2026-01-01T00:00:00"),
    ]),
]


@pytest.fixture(autouse=True)
def gpc_fixture_db(tmp_path, monkeypatch):
    """Point app.database at a small fixture DB for the duration of a test."""
    db_path = tmp_path / "gpc_fixture.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(_FIXTURE_SCHEMA)
    for sql, rows in _FIXTURE_ROWS:
        conn.executemany(sql, rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_db", None)
    yield
    # Drop the connection so the next test reconnects to its own fixture
    database._db = None


# ── Segments ──────────────────────────────────────────────────────────

def test_list_segments():
    body = client.get("/api/gpc/segments/").json()
    assert body["count"] == 2
    assert body["results"][0] == {
        "segment_code": "50000000", "description": "Food/Beverage",
    }


def test_list_segments_search_filter():
    body = client.get("/api/gpc/segments/", params={"search": "Health"}).json()
    assert body["count"] == 1
    assert body["results"][0]["segment_code"] == "51000000"


def test_segment_detail_includes_families():
    body = client.get("/api/gpc/segments/50000000").json()
    assert body["description"] == "Food/Beverage"
    assert [f["family_code"] for f in body["families"]] == ["50100000", "50200000"]


def test_segment_detail_404():
    assert client.get("/api/gpc/segments/99999999").status_code == 404


# ── Families / Classes ────────────────────────────────────────────────

def test_list_families_filtered_by_segment():
    body = client.get("/api/gpc/families/", params={"segment_code": "50000000"}).json()
    assert body["count"] == 2
    body = client.get("/api/gpc/families/", params={"segment_code": "51000000"}).json()
    assert body["count"] == 0


def test_class_detail_breadcrumb():
    body = client.get("/api/gpc/classes/50202300").json()
    assert body["full_path"] == "Food/Beverage > Beverages > Carbonated Drinks"
    assert [b["brick_code"] for b in body["bricks"]] == ["10000201", "10000202"]
    assert body["family_code_details"]["segment_code"] == "50000000"


# ── Bricks ────────────────────────────────────────────────────────────

def test_list_bricks_pagination():
    body = client.get("/api/gpc/bricks/", params={"page_size": 2}).json()
    assert body["count"] == 3
    assert len(body["results"]) == 2
    assert "page=2" in body["next"]
    assert body["previous"] is None

    page2 = client.get("/api/gpc/bricks/", params={"page_size": 2, "page": 2}).json()
    assert len(page2["results"]) == 1
    assert page2["next"] is None
    assert "page=1" in page2["previous"]


def test_brick_detail_with_attributes():
    body = client.get("/api/gpc/bricks/10000201").json()
    assert body["full_path"] == (
        "Food/Beverage > Beverages > Carbonated Drinks > Cola Drinks"
    )
    assert len(body["attributes"]) == 1
    att = body["attributes"][0]
    assert att["att_type_text"] == "Caffeine Presence"
    assert [v["att_value_text"] for v in att["values"]] == [
        "Caffeinated", "Decaffeinated",
    ]


def test_brick_without_attributes_returns_empty_list():
    body = client.get("/api/gpc/bricks/10005900").json()
    assert body["attributes"] == []


def test_brick_detail_404():
    assert client.get("/api/gpc/bricks/00000000").status_code == 404


# ── Search ────────────────────────────────────────────────────────────

def test_search_across_entities():
    body = client.get("/api/gpc/search/", params={"q": "Beverage"}).json()
    assert [s["segment_code"] for s in body["segments"]] == ["50000000"]
    assert [f["family_code"] for f in body["families"]] == ["50200000"]
    assert body["classes"] == []
    assert body["bricks"] == []


def test_search_category_filter():
    body = client.get(
        "/api/gpc/search/", params={"q": "Cola", "category": "bricks"},
    ).json()
    assert [b["brick_code"] for b in body["bricks"]] == ["10000201"]
    assert body["segments"] == []


def test_search_empty_query_returns_empty_response():
    body = client.get("/api/gpc/search/").json()
    assert body == {"segments": [], "families": [], "classes": [], "bricks": []}
