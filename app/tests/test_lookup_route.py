"""
Tests for the /api/v1/lookup/{gtin} endpoint contract.

Uses TestClient without triggering the lifespan (no GPC database build),
with the orchestrator monkeypatched so no network access is required.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core import orchestrator
from app.core.models import CanonicalProduct, NutrientValue
from app.main import app

client = TestClient(app)


@pytest.fixture
def canned_lookup(monkeypatch):
    """Make orchestrator.lookup return a fixed product without network calls."""
    product = CanonicalProduct(
        gtin="04963406021372",
        product_name="Coca-Cola Classic",
        brand="The Coca-Cola Company",
        category_hierarchy=["Food/Beverage", "Beverages"],
        calories_kcal=42.0,
        carbohydrates=NutrientValue(value=10.6),
        data_sources=["USDA_FDC", "OpenFoodFacts", "GS1_GPC"],
        upstream_latency_ms={"USDA_FDC": 110.5},
    )

    async def fake_lookup(gtin, *a, **k):
        return product

    monkeypatch.setattr(orchestrator, "lookup", fake_lookup)
    return product


def test_lookup_returns_canonical_product(canned_lookup):
    resp = client.get("/api/v1/lookup/04963406021372")

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_name"] == "Coca-Cola Classic"
    assert body["carbohydrates"] == {"value": 10.6, "unit": "g"}
    assert body["data_sources"] == ["USDA_FDC", "OpenFoodFacts", "GS1_GPC"]


def test_lookup_404_when_no_source_has_data(monkeypatch):
    async def fake_lookup(gtin, *a, **k):
        return CanonicalProduct(gtin=gtin)

    monkeypatch.setattr(orchestrator, "lookup", fake_lookup)

    resp = client.get("/api/v1/lookup/00000000000000")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "bad_gtin",
    [
        "abc",              # not numeric
        "12345",            # too short
        "123456789",        # 9 digits — not a valid GTIN length
        "123456789012345",  # 15 digits — too long
        "0496340602137X",   # trailing letter
    ],
)
def test_lookup_rejects_malformed_gtin(bad_gtin):
    resp = client.get(f"/api/v1/lookup/{bad_gtin}")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "good_gtin",
    [
        "12345678",        # GTIN-8
        "123456789012",    # GTIN-12 (UPC-A)
        "1234567890123",   # GTIN-13 (EAN-13)
        "12345678901234",  # GTIN-14
    ],
)
def test_lookup_accepts_valid_gtin_lengths(good_gtin, canned_lookup):
    resp = client.get(f"/api/v1/lookup/{good_gtin}")
    assert resp.status_code == 200


def test_fresh_query_param_is_forwarded_to_the_orchestrator(monkeypatch):
    """?fresh=true must reach orchestrator.lookup as fresh=True."""
    seen = {}

    async def fake_lookup(gtin, fresh=False):
        seen["fresh"] = fresh
        return CanonicalProduct(gtin=gtin, data_sources=["USDA_FDC"])

    monkeypatch.setattr(orchestrator, "lookup", fake_lookup)

    client.get("/api/v1/lookup/028400642255?fresh=true")
    assert seen["fresh"] is True

    client.get("/api/v1/lookup/028400642255")
    assert seen["fresh"] is False
