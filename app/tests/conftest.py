"""
Shared test fixtures.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest

from app.core import orchestrator
from app.core import resilience


@pytest.fixture(autouse=True)
def reset_shared_state():
    """Clear the lookup cache and reset circuit breakers between tests."""
    orchestrator._lookup_cache.clear()
    for breaker in (resilience.usda_breaker, resilience.off_breaker):
        breaker.record_success()
    yield
    orchestrator._lookup_cache.clear()


@pytest.fixture
def off_product():
    """A representative formatted Open Food Facts product."""
    return {
        "barcode": "04963406021372",
        "product_name": "Coca-Cola Classic",
        "brands": "Coca-Cola",
        "image_url": "https://images.example.org/coke.jpg",
        "ingredients_text": "Carbonated water, high fructose corn syrup",
        "categories": ["en:beverages", "en:carbonated-drinks"],
        "allergens": [],
        "labels": [],
        "nutrients_per_100g": {
            "calories_kcal": 44.0,
            "protein_g": 0.1,
            "fat_g": 0.2,
            "carbohydrates_g": 11.0,
            "sugars_g": 10.6,
        },
    }


@pytest.fixture
def usda_food():
    """A representative formatted USDA FDC branded food."""
    return {
        "fdc_id": 123456,
        "description": "COCA-COLA CLASSIC",
        "brand_owner": "The Coca-Cola Company",
        "brand_name": None,
        "ingredients": "CARBONATED WATER, HIGH FRUCTOSE CORN SYRUP, CARAMEL COLOR",
        "nutrients": {
            "Energy": {"amount": 42.0, "unit": "KCAL"},
            "Protein": {"amount": 0.0, "unit": "G"},
            "Total lipid (fat)": {"amount": 0.0, "unit": "G"},
            "Carbohydrate, by difference": {"amount": 10.6, "unit": "G"},
            "Sodium, Na": {"amount": 4.0, "unit": "MG"},
        },
    }
