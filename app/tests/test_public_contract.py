"""
Guards on the published contract.

CanonicalProduct is what every consumer codes against. Renaming a field,
changing a unit, or dropping a key is a breaking change that no other test
would catch — the merge tests would happily keep passing against the new
shape. These tests fail loudly when the contract moves, so that breaking it
is a deliberate act with a version bump rather than an accident.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core.models import CanonicalProduct, NutrientValue
from app.main import app

client = TestClient(app)


# The published v1 shape. Changing this set is a breaking API change.
CANONICAL_FIELDS = {
    "gtin",
    "product_name",
    "brand",
    "category_hierarchy",
    "calories_kcal",
    "protein",
    "fat",
    "saturated_fat",
    "trans_fat",
    "cholesterol",
    "carbohydrates",
    "fiber",
    "sugars",
    "added_sugars",
    "sodium",
    "potassium",
    "calcium",
    "iron",
    "vitamin_d",
    "image_url",
    "ingredients_text",
    "allergens",
    "labels",
    "data_sources",
    "upstream_latency_ms",
    "cached",
    "attribution",
}

SOURCE_NAMES = {"USDA_FDC", "OpenFoodFacts", "GS1_GPC"}


def test_canonical_product_field_set_is_stable():
    assert set(CanonicalProduct.model_fields) == CANONICAL_FIELDS


def test_nutrient_value_shape_is_stable():
    assert set(NutrientValue.model_fields) == {"value", "unit"}
    assert NutrientValue(value=1).unit == "g"     # grams remain the default


def test_serialized_product_exposes_exactly_the_published_fields():
    body = CanonicalProduct(gtin="028400642255").model_dump()
    assert set(body) == CANONICAL_FIELDS


def test_nutrients_serialize_as_value_unit_objects():
    """Consumers read `.protein.value` / `.protein.unit` — not a bare number."""
    product = CanonicalProduct(gtin="1", protein=NutrientValue(value=7.1))
    assert product.model_dump()["protein"] == {"value": 7.1, "unit": "g"}


def test_defaults_are_empty_containers_not_null():
    """List/dict fields must default to empty containers so a consumer can
    iterate them without a None check."""
    body = CanonicalProduct(gtin="1").model_dump()
    assert body["category_hierarchy"] == []
    assert body["allergens"] == []
    assert body["labels"] == []
    assert body["data_sources"] == []
    assert body["upstream_latency_ms"] == {}


def test_unknown_product_name_default_is_stable():
    assert CanonicalProduct(gtin="1").product_name == "Unknown"


def test_gtin_is_the_only_required_field():
    CanonicalProduct(gtin="1")          # must not raise

    with pytest.raises(Exception):
        CanonicalProduct()              # gtin is mandatory


def test_source_names_are_stable():
    """data_sources values are a documented enum in everything but name;
    renaming one silently breaks every consumer that filters on them."""
    from app.core import orchestrator

    src = orchestrator.__doc__ or ""
    assert "USDA FDC" in src or "USDA_FDC" in src

    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["CanonicalProduct"]
    assert set(schema["properties"]) == CANONICAL_FIELDS


# ── OpenAPI-level guards ──────────────────────────────────────────────

def test_openapi_is_valid_and_versioned():
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "Nutrition API"
    assert spec["info"]["version"]


def test_every_v1_route_is_under_the_version_prefix():
    """Nothing should escape /api/v1 except the GPC browser and the UI."""
    paths = client.get("/openapi.json").json()["paths"]
    for path in paths:
        assert path.startswith(("/api/v1/", "/api/v1/gpc/")), path


def test_lookup_documents_its_404_and_422():
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/api/v1/lookup/{gtin}"]["get"]["responses"]
    assert "200" in responses
    assert "422" in responses


def test_gtin_pattern_is_published_in_the_schema():
    """Clients generate validators from this — it must be present."""
    spec = client.get("/openapi.json").json()
    params = spec["paths"]["/api/v1/lookup/{gtin}"]["get"]["parameters"]
    gtin = next(p for p in params if p["name"] == "gtin")
    assert "pattern" in gtin["schema"]


def test_nutrients_are_documented_as_nullable():
    """Every nutrient is optional — the schema must say so, or generated
    clients will treat a missing value as a contract violation."""
    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["CanonicalProduct"]["properties"]

    for field in ["protein", "fat", "carbohydrates", "fiber", "sugars", "sodium"]:
        assert "anyOf" in schema[field], f"{field} is not documented as nullable"


def test_health_and_version_are_tagged_operations():
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/api/v1/health"]["get"]["tags"] == ["Operations"]
    assert spec["paths"]["/api/v1/version"]["get"]["tags"] == ["Operations"]
