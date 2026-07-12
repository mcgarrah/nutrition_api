"""
Tests for the canonical Pydantic models.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from pydantic import ValidationError

from app.core.models import CanonicalProduct, NutrientValue


def test_nutrient_value_defaults_to_grams():
    nv = NutrientValue(value=10.6)
    assert nv.unit == "g"


def test_nutrient_value_requires_numeric_value():
    with pytest.raises(ValidationError):
        NutrientValue(value="not a number")


def test_canonical_product_minimal():
    product = CanonicalProduct(gtin="123456789012")
    assert product.product_name == "Unknown"
    assert product.category_hierarchy == []
    assert product.data_sources == []
    assert product.calories_kcal is None


def test_canonical_product_list_defaults_are_independent():
    a = CanonicalProduct(gtin="1")
    b = CanonicalProduct(gtin="2")
    a.data_sources.append("USDA_FDC")
    assert b.data_sources == []
