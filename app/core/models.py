"""
Canonical data models for the unified food intelligence API.

These models represent the single source of truth returned by the
/api/v1/lookup/{gtin} endpoint, abstracting away the differences
between USDA FDC, Open Food Facts, and GS1 GPC data sources.

All nutrient values are normalized to per-100g basis.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from pydantic import BaseModel, Field, HttpUrl


class NutrientValue(BaseModel):
    value: float
    unit: str = "g"


class CanonicalProduct(BaseModel):
    """Unified product representation merging data from all sources."""

    gtin: str = Field(description="The product barcode (UPC/EAN/GTIN)")
    product_name: str = Field(default="Unknown", description="Unified product name")
    brand: str | None = None

    # GS1 GPC taxonomy
    category_hierarchy: list[str] = Field(
        default_factory=list,
        description="GS1 category path: Segment > Family > Class > Brick",
    )

    # Normalized nutrition facts (per 100g or 100mL)
    calories_kcal: float | None = None
    protein: NutrientValue | None = None
    fat: NutrientValue | None = None
    carbohydrates: NutrientValue | None = None
    fiber: NutrientValue | None = None
    sugars: NutrientValue | None = None
    sodium: NutrientValue | None = None

    # Product metadata from OFF (images, ingredients, labels)
    image_url: str | None = None
    ingredients_text: str | None = None
    allergens: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    # Data governance
    data_sources: list[str] = Field(
        default_factory=list,
        description="Which upstream sources contributed data",
    )
    upstream_latency_ms: dict[str, float] = Field(
        default_factory=dict,
        description="Response time from each upstream source in milliseconds",
    )
