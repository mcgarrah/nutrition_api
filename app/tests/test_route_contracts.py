"""
End-to-end contract tests for the public HTTP surface.

These cover what a consumer actually sees: status codes, the shape of the
response body, and the promise that an upstream outage degrades to a partial
200 rather than a 500.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import orchestrator, resilience, usda_fdc
from app.core.models import CanonicalProduct, NutrientValue
from app.main import app

client = TestClient(app)


# ── /api/v1/lookup/{gtin} ─────────────────────────────────────────────

@pytest.fixture
def stub_lookup(monkeypatch):
    def install(product):
        async def fake(gtin):
            return product

        monkeypatch.setattr(orchestrator, "lookup", fake)

    return install


def test_lookup_serializes_the_full_canonical_product(stub_lookup):
    stub_lookup(CanonicalProduct(
        gtin="028400642255",
        product_name="Doritos",
        brand="Frito-Lay",
        category_hierarchy=["Food/Beverage", "Snacks"],
        calories_kcal=536.0,
        protein=NutrientValue(value=7.1),
        sodium=NutrientValue(value=530.0, unit="mg"),
        image_url="https://img.example/d.jpg",
        ingredients_text="CORN, OIL",
        allergens=["en:milk"],
        labels=["en:no-gluten"],
        data_sources=["OpenFoodFacts", "USDA_FDC", "GS1_GPC"],
        upstream_latency_ms={"USDA_FDC": 110.5},
    ))

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["product_name"] == "Doritos"
    assert body["protein"] == {"value": 7.1, "unit": "g"}
    assert body["sodium"] == {"value": 530.0, "unit": "mg"}
    assert body["category_hierarchy"] == ["Food/Beverage", "Snacks"]
    assert body["data_sources"] == ["OpenFoodFacts", "USDA_FDC", "GS1_GPC"]
    assert body["upstream_latency_ms"] == {"USDA_FDC": 110.5}


def test_absent_nutrients_serialize_as_null_not_omitted(stub_lookup):
    """Consumers index these keys directly; they must always be present."""
    stub_lookup(CanonicalProduct(gtin="028400642255", data_sources=["OpenFoodFacts"]))

    body = client.get("/api/v1/lookup/028400642255").json()

    for key in ["protein", "fat", "carbohydrates", "fiber", "sugars",
                "sodium", "calories_kcal", "image_url", "brand"]:
        assert key in body
        assert body[key] is None


def test_lookup_404_when_no_source_had_data(stub_lookup):
    stub_lookup(CanonicalProduct(gtin="00000000"))
    resp = client.get("/api/v1/lookup/00000000")

    assert resp.status_code == 404
    assert "00000000" in resp.json()["detail"]


@pytest.mark.parametrize("gtin", [
    "12345678",        # GTIN-8
    "123456789012",    # GTIN-12 / UPC-A
    "1234567890123",   # GTIN-13 / EAN-13
    "12345678901234",  # GTIN-14
])
def test_valid_gtin_lengths_accepted(gtin, stub_lookup):
    stub_lookup(CanonicalProduct(gtin=gtin, data_sources=["OpenFoodFacts"]))
    assert client.get(f"/api/v1/lookup/{gtin}").status_code == 200


@pytest.mark.parametrize("gtin", [
    "abc",               # letters
    "1234567",           # 7 digits — no such GTIN
    "123456789",         # 9 digits
    "12345678901",       # 11 digits
    "123456789012345",   # 15 digits
    "0284-0064-2255",    # punctuation
    "028400642255 ",     # trailing space
])
def test_malformed_gtins_rejected_before_any_upstream_call(gtin, monkeypatch):
    """Validation must happen at the edge — a bad barcode never reaches USDA/OFF."""
    called = {"n": 0}

    async def should_not_run(g):
        called["n"] += 1
        return CanonicalProduct(gtin=g)

    monkeypatch.setattr(orchestrator, "lookup", should_not_run)

    assert client.get(f"/api/v1/lookup/{gtin}").status_code == 422
    assert called["n"] == 0


# ── graceful degradation (the headline promise) ───────────────────────

def test_upstream_outage_degrades_to_partial_200_not_500(monkeypatch, off_product, gpc_db):
    """USDA down + OFF up must still answer, flagged via data_sources."""
    async def off_ok(barcode):
        return off_product

    async def usda_down(upc):
        raise ConnectionError("FDC unreachable")

    monkeypatch.setattr(off, "get_product", off_ok)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_down)

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    body = resp.json()
    assert "OpenFoodFacts" in body["data_sources"]
    assert "USDA_FDC" not in body["data_sources"]        # absent, not fatal
    assert body["calories_kcal"] == 44.0                 # OFF's provisional value
    assert "USDA_FDC" in body["upstream_latency_ms"]     # the attempt is still timed


def test_both_upstreams_down_yields_404_not_500(monkeypatch, gpc_db):
    async def down(*a):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(off, "get_product", down)
    monkeypatch.setattr(usda_fdc, "search_by_upc", down)

    assert client.get("/api/v1/lookup/028400642255").status_code == 404


def test_slow_upstream_is_cut_off_at_the_timeout(monkeypatch, off_product, gpc_db):
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.05)

    async def off_ok(barcode):
        return off_product

    async def usda_slow(upc):
        await asyncio.sleep(5)

    monkeypatch.setattr(off, "get_product", off_ok)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_slow)

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    assert "USDA_FDC" not in resp.json()["data_sources"]


def test_open_circuit_is_skipped_without_delaying_the_request(monkeypatch, off_product, gpc_db):
    async def off_ok(barcode):
        return off_product

    calls = {"n": 0}

    async def usda_should_be_skipped(upc):
        calls["n"] += 1
        return None

    monkeypatch.setattr(off, "get_product", off_ok)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_should_be_skipped)
    for _ in range(resilience.usda_breaker.failure_threshold):
        resilience.usda_breaker.record_failure()

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    assert calls["n"] == 0


# ── /api/v1/usda/* ────────────────────────────────────────────────────

def test_usda_search_requires_a_query():
    assert client.get("/api/v1/usda/search").status_code == 422


@pytest.mark.parametrize("page_size", [0, 201])
def test_usda_search_page_size_bounds(page_size):
    resp = client.get("/api/v1/usda/search", params={"q": "cola", "page_size": page_size})
    assert resp.status_code == 422


def test_usda_search_returns_results(monkeypatch):
    async def ok(q, page_size=25):
        return {"total_hits": 1, "foods": [{"fdc_id": 1, "description": "COLA"}]}

    monkeypatch.setattr(usda_fdc, "search", ok)
    body = client.get("/api/v1/usda/search", params={"q": "cola"}).json()
    assert body["total_hits"] == 1


def test_usda_food_by_id(monkeypatch):
    async def ok(fdc_id):
        return {"fdc_id": fdc_id, "description": "COLA"}

    monkeypatch.setattr(usda_fdc, "get_food", ok)
    assert client.get("/api/v1/usda/food/123").json()["fdc_id"] == 123


def test_usda_food_rejects_non_numeric_id():
    assert client.get("/api/v1/usda/food/abc").status_code == 422


def test_usda_food_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(usda_fdc, "is_available", lambda: False)
    assert client.get("/api/v1/usda/food/123").status_code == 503


def test_usda_food_404_when_the_food_does_not_exist(monkeypatch):
    """A question with no answer is a 404. It used to be reported as 503 —
    blaming the service for the absence of a food nobody had."""
    async def missing(fdc_id):
        return None

    monkeypatch.setattr(usda_fdc, "is_available", lambda: True)
    monkeypatch.setattr(usda_fdc, "get_food", missing)

    resp = client.get("/api/v1/usda/food/999999999")
    assert resp.status_code == 404
    assert "999999999" in resp.json()["detail"]


def test_usda_food_502_on_upstream_error(monkeypatch):
    async def boom(fdc_id):
        raise ConnectionError("down")

    monkeypatch.setattr(usda_fdc, "is_available", lambda: True)
    monkeypatch.setattr(usda_fdc, "get_food", boom)
    assert client.get("/api/v1/usda/food/123").status_code == 502


def test_usda_upc_lookup_returns_food(monkeypatch):
    async def ok(upc):
        return {"fdc_id": 1, "description": "DORITOS"}

    monkeypatch.setattr(usda_fdc, "search_by_upc", ok)
    assert client.get("/api/v1/usda/lookup/028400642255").json()["description"] == "DORITOS"


def test_usda_upc_lookup_502_on_upstream_error(monkeypatch):
    async def boom(upc):
        raise ConnectionError("down")

    monkeypatch.setattr(usda_fdc, "search_by_upc", boom)
    assert client.get("/api/v1/usda/lookup/028400642255").status_code == 502


# ── /api/v1/off/* ─────────────────────────────────────────────────────

def test_off_product_returns_payload(monkeypatch, off_product):
    async def ok(barcode):
        return off_product

    monkeypatch.setattr(off, "get_product", ok)
    body = client.get("/api/v1/off/product/3017620422003").json()
    assert body["product_name"] == "Coca-Cola Classic"


def test_off_search_requires_a_query():
    assert client.get("/api/v1/off/search").status_code == 422


@pytest.mark.parametrize("page_size", [0, 101])
def test_off_search_page_size_bounds(page_size):
    resp = client.get("/api/v1/off/search", params={"q": "cola", "page_size": page_size})
    assert resp.status_code == 422


def test_off_search_returns_results(monkeypatch, off_product):
    async def ok(q, page_size=25):
        return {"total": 1, "products": [off_product]}

    monkeypatch.setattr(off, "search", ok)
    assert client.get("/api/v1/off/search", params={"q": "cola"}).json()["total"] == 1


def test_off_search_503_when_unavailable(monkeypatch):
    async def unavailable(q, page_size=25):
        return None

    monkeypatch.setattr(off, "search", unavailable)
    assert client.get("/api/v1/off/search", params={"q": "cola"}).status_code == 503


def test_off_search_502_on_upstream_error(monkeypatch):
    async def boom(q, page_size=25):
        raise ConnectionError("down")

    monkeypatch.setattr(off, "search", boom)
    assert client.get("/api/v1/off/search", params={"q": "cola"}).status_code == 502
