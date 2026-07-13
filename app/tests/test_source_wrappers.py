"""
Tests for the OFF/USDA wrapper formatting helpers and the error mapping
on the source-specific routes (not-found vs upstream error vs unconfigured).

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio

from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import orchestrator, resilience, usda_fdc
from app.main import app

client = TestClient(app)


# ── OFF formatting helpers ────────────────────────────────────────────

def test_extract_nutrients_maps_per_100g_keys():
    nutriments = {
        "energy-kcal_100g": 44.0,
        "proteins_100g": 0.1,
        "fat_100g": 0.0,
        "carbohydrates_100g": 11.0,
        "sugars_100g": 10.6,
        "unrelated_key": 99,
    }
    result = off._extract_nutrients(nutriments)
    assert result == {
        "calories_kcal": 44.0,
        "protein_g": 0.1,
        "fat_g": 0.0,
        "carbohydrates_g": 11.0,
        "sugars_g": 10.6,
    }


def test_format_product_shapes_off_response():
    raw = {
        "code": "123",
        "product_name": "Test Cola",
        "brands": "TestBrand",
        "nutriments": {"fat_100g": 1.5},
        "categories_tags": ["en:beverages"],
    }
    result = off._format_product(raw)
    assert result["barcode"] == "123"
    assert result["product_name"] == "Test Cola"
    assert result["categories"] == ["en:beverages"]
    assert result["nutrients_per_100g"] == {"fat_g": 1.5}
    # Absent fields come through as None / empty, never KeyError
    assert result["image_url"] is None
    assert result["allergens"] == []


# ── Route error mapping ───────────────────────────────────────────────

def test_off_product_not_found_maps_to_404(monkeypatch):
    async def not_found(barcode):
        return None

    monkeypatch.setattr(off, "get_product", not_found)
    assert client.get("/api/v1/off/product/123").status_code == 404


def test_off_product_upstream_error_maps_to_502(monkeypatch):
    async def boom(barcode):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(off, "get_product", boom)
    assert client.get("/api/v1/off/product/123").status_code == 502


def test_usda_search_unconfigured_maps_to_503(monkeypatch):
    async def unconfigured(q, page_size=25):
        return None

    monkeypatch.setattr(usda_fdc, "search", unconfigured)
    assert client.get("/api/v1/usda/search", params={"q": "cola"}).status_code == 503


def test_usda_search_upstream_error_maps_to_502(monkeypatch):
    async def boom(q, page_size=25):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(usda_fdc, "search", boom)
    assert client.get("/api/v1/usda/search", params={"q": "cola"}).status_code == 502


def test_usda_upc_lookup_not_found_maps_to_404(monkeypatch):
    async def not_found(upc):
        return None

    monkeypatch.setattr(usda_fdc, "search_by_upc", not_found)
    assert client.get("/api/v1/usda/lookup/123456789012").status_code == 404


# ── Orchestrator fetch resilience ─────────────────────────────────────

async def test_fetch_off_absorbs_upstream_exception(monkeypatch):
    async def boom(barcode):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(off, "get_product", boom)
    data, latency_ms = await orchestrator._fetch_off("123")
    assert data is None
    assert latency_ms >= 0
    # The breaker saw the failure
    assert resilience.off_breaker._consecutive_failures == 1


async def test_fetch_off_skips_when_circuit_open(monkeypatch):
    calls = {"n": 0}

    async def counting(barcode):
        calls["n"] += 1
        return None

    monkeypatch.setattr(off, "get_product", counting)
    for _ in range(resilience.off_breaker.failure_threshold):
        resilience.off_breaker.record_failure()

    data, _ = await orchestrator._fetch_off("123")
    assert data is None
    assert calls["n"] == 0  # never reached the upstream


async def test_fetch_usda_times_out_slow_upstream(monkeypatch):
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.05)

    async def slow(upc):
        await asyncio.sleep(1.0)

    monkeypatch.setattr(usda_fdc, "search_by_upc", slow)
    data, latency_ms = await orchestrator._fetch_usda("123")
    assert data is None
    assert latency_ms < 500  # returned at the timeout, not after the full sleep
