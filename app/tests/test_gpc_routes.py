"""
Tests for the GPC browser routes, backed by a small fixture SQLite database.

The fixture database mirrors the production schema (including the junction
tables for brick attributes) with a handful of rows, so these tests exercise
the real SQL without needing the full GS1 import.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Every test in this module queries the fixture GPC database
pytestmark = pytest.mark.usefixtures("gpc_db")


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
