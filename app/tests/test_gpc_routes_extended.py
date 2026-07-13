"""
Coverage for the GPC browser routes not exercised by test_gpc_routes:
family/class listings with their filters, breadcrumb construction, and the
pagination/validation contract shared by every list endpoint.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("gpc_db")


# ── Families ──────────────────────────────────────────────────────────

def test_list_families_unfiltered():
    body = client.get("/api/gpc/families/").json()
    assert body["count"] == 2
    assert [f["family_code"] for f in body["results"]] == ["50100000", "50200000"]


def test_list_families_search_matches_description():
    body = client.get("/api/gpc/families/", params={"search": "Bever"}).json()
    assert [f["family_code"] for f in body["results"]] == ["50200000"]


def test_list_families_search_matches_code():
    body = client.get("/api/gpc/families/", params={"search": "50200000"}).json()
    assert body["count"] == 1


def test_list_families_search_and_segment_filter_combine():
    """Both clauses must AND together, not replace each other."""
    hit = client.get(
        "/api/gpc/families/", params={"search": "Bever", "segment_code": "50000000"},
    ).json()
    assert hit["count"] == 1

    miss = client.get(
        "/api/gpc/families/", params={"search": "Bever", "segment_code": "51000000"},
    ).json()
    assert miss["count"] == 0


def test_family_detail_has_breadcrumb_and_children():
    body = client.get("/api/gpc/families/50200000").json()

    assert body["description"] == "Beverages"
    assert body["full_path"] == "Food/Beverage > Beverages"
    assert body["segment_code_details"] == {
        "segment_code": "50000000", "segment_description": "Food/Beverage",
    }
    assert [c["class_code"] for c in body["classes"]] == ["50202300"]


def test_family_detail_404():
    assert client.get("/api/gpc/families/99999999").status_code == 404


# ── Classes ───────────────────────────────────────────────────────────

def test_list_classes_unfiltered():
    body = client.get("/api/gpc/classes/").json()
    assert body["count"] == 2


def test_list_classes_filtered_by_family():
    body = client.get("/api/gpc/classes/", params={"family_code": "50200000"}).json()
    assert [c["class_code"] for c in body["results"]] == ["50202300"]


def test_list_classes_search():
    body = client.get("/api/gpc/classes/", params={"search": "Carbonated"}).json()
    assert body["count"] == 1


def test_class_detail_404():
    assert client.get("/api/gpc/classes/99999999").status_code == 404


def test_class_with_no_bricks_returns_empty_list(gpc_db):
    import sqlite3
    conn = sqlite3.connect(gpc_db)
    conn.execute("INSERT INTO classes VALUES ('50209999', 'Empty Class', '50200000')")
    conn.commit()
    conn.close()

    body = client.get("/api/gpc/classes/50209999").json()
    assert body["bricks"] == []
    assert body["full_path"] == "Food/Beverage > Beverages > Empty Class"


# ── Bricks ────────────────────────────────────────────────────────────

def test_list_bricks_filtered_by_class():
    body = client.get("/api/gpc/bricks/", params={"class_code": "50202300"}).json()
    assert [b["brick_code"] for b in body["results"]] == ["10000201", "10000202"]


def test_list_bricks_search_and_class_filter_combine():
    body = client.get(
        "/api/gpc/bricks/", params={"search": "Cola", "class_code": "50202300"},
    ).json()
    assert body["count"] == 1

    body = client.get(
        "/api/gpc/bricks/", params={"search": "Cola", "class_code": "50101800"},
    ).json()
    assert body["count"] == 0


# ── Pagination contract (shared by every list endpoint) ───────────────

def test_first_page_has_no_previous_link():
    body = client.get("/api/gpc/bricks/", params={"page_size": 1}).json()
    assert body["previous"] is None
    assert "page=2" in body["next"]


def test_last_page_has_no_next_link():
    body = client.get("/api/gpc/bricks/", params={"page_size": 1, "page": 3}).json()
    assert body["next"] is None
    assert "page=2" in body["previous"]


def test_middle_page_has_both_links():
    body = client.get("/api/gpc/bricks/", params={"page_size": 1, "page": 2}).json()
    assert body["next"] is not None
    assert body["previous"] is not None


def test_page_beyond_the_end_is_empty_not_an_error():
    body = client.get("/api/gpc/bricks/", params={"page": 99}).json()
    assert body["results"] == []
    assert body["count"] == 3       # total is still reported
    assert body["next"] is None


def test_pagination_links_preserve_page_size():
    body = client.get("/api/gpc/bricks/", params={"page_size": 2}).json()
    assert "page_size=2" in body["next"]


def test_count_is_the_total_not_the_page_length():
    body = client.get("/api/gpc/bricks/", params={"page_size": 1}).json()
    assert body["count"] == 3
    assert len(body["results"]) == 1


@pytest.mark.parametrize("params", [
    {"page": 0},          # page must be >= 1
    {"page": -1},
    {"page_size": 0},     # page_size must be >= 1
    {"page_size": 101},   # ...and <= 100
])
def test_invalid_pagination_params_are_rejected(params):
    assert client.get("/api/gpc/bricks/", params=params).status_code == 422


# ── Search endpoint ───────────────────────────────────────────────────

@pytest.mark.parametrize("category,expected_key", [
    ("segments", "segments"),
    ("families", "families"),
    ("classes", "classes"),
    ("bricks", "bricks"),
])
def test_search_category_returns_only_that_entity(category, expected_key):
    body = client.get(
        "/api/gpc/search/", params={"q": "0", "category": category},
    ).json()
    others = [k for k in ("segments", "families", "classes", "bricks") if k != expected_key]
    assert body[expected_key] != []
    assert all(body[k] == [] for k in others)


def test_search_matches_on_code_as_well_as_text():
    body = client.get("/api/gpc/search/", params={"q": "10000201"}).json()
    assert [b["brick_code"] for b in body["bricks"]] == ["10000201"]


def test_search_no_match_returns_empty_lists():
    body = client.get("/api/gpc/search/", params={"q": "zzzznothing"}).json()
    assert body == {"segments": [], "families": [], "classes": [], "bricks": []}


def test_search_rejects_an_unknown_category():
    """The OpenAPI schema advertises an enum; the server must enforce it
    rather than silently returning empty results for a typo."""
    resp = client.get("/api/gpc/search/", params={"q": "cola", "category": "bogus"})
    assert resp.status_code == 422


def test_search_category_enum_is_published_in_the_schema():
    spec = client.get("/openapi.json").json()
    params = spec["paths"]["/api/gpc/search/"]["get"]["parameters"]
    category = next(p for p in params if p["name"] == "category")
    assert set(category["schema"]["enum"]) == {
        "all", "segments", "families", "classes", "bricks",
    }


# ── Pagination links must preserve the caller's filters ───────────────

def test_next_link_preserves_a_code_filter():
    """Following `next` on a filtered list must stay inside that filter.
    Rebuilding the URL from the path alone dropped it, so page 2 silently
    returned rows from the whole taxonomy — a different set than the `count`
    beside it described."""
    body = client.get(
        "/api/gpc/bricks/", params={"class_code": "50202300", "page_size": 1},
    ).json()

    assert body["count"] == 2                   # Cola Drinks + Lemonade
    assert "class_code=50202300" in body["next"]

    page2 = client.get(body["next"]).json()
    assert page2["count"] == 2                  # still the filtered total
    assert page2["results"][0]["brick_code"] == "10000202"   # Lemonade, not a
    assert "class_code=50202300" in page2["previous"]        # random brick


def test_next_link_preserves_a_search_filter():
    # "e" matches Lemonade and Apples, but not Cola Drinks — two results
    body = client.get(
        "/api/gpc/bricks/", params={"search": "e", "page_size": 1},
    ).json()

    assert body["count"] == 2
    assert "search=e" in body["next"]

    page2 = client.get(body["next"]).json()
    assert page2["count"] == 2                  # still the filtered total
    assert page2["results"][0]["brick_code"] == "10005900"   # Apples


def test_pagination_links_preserve_combined_filters():
    body = client.get(
        "/api/gpc/families/",
        params={"search": "e", "segment_code": "50000000", "page_size": 1},
    ).json()

    assert "search=e" in body["next"]
    assert "segment_code=50000000" in body["next"]

    page2 = client.get(body["next"]).json()
    assert page2["count"] == body["count"]


def test_following_next_then_previous_returns_the_first_page():
    """The round trip a paging client actually performs."""
    first = client.get(
        "/api/gpc/bricks/", params={"class_code": "50202300", "page_size": 1},
    ).json()
    second = client.get(first["next"]).json()
    back = client.get(second["previous"]).json()

    assert back["results"] == first["results"]
    assert back["count"] == first["count"]


def test_paging_links_do_not_duplicate_parameters():
    """include_query_params must override page/page_size, not append to them."""
    body = client.get("/api/gpc/bricks/", params={"page": 1, "page_size": 1}).json()

    assert body["next"].count("page=") == 1
    assert body["next"].count("page_size=") == 1
    assert "page=2" in body["next"]
