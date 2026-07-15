"""
Canonical data models for the unified food intelligence API.

These models represent the single source of truth returned by the
/api/v1/lookup/{gtin} endpoint, abstracting away the differences
between USDA FDC, Open Food Facts, and GS1 GPC data sources.

All nutrient values are normalized to per-100g basis.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from pydantic import BaseModel, Field


class NutrientValue(BaseModel):
    value: float
    unit: str = "g"


class SourceProvenance(BaseModel):
    """Where a source's data actually came from on this request.

    A source can be served from the local bulk copy (fast, and only as fresh as
    the last import) or from the live upstream API. This records which, and — for
    a local copy — the date of the dataset it was built from, so a caller can see
    how old the answer is.
    """

    origin: str = Field(description="'local' (bulk copy on disk) or 'live' (upstream API)")
    dataset: str | None = Field(
        default=None, description="The local dataset the answer came from, if local"
    )
    dataset_date: str | None = Field(
        default=None, description="Publication date (YYYY-MM-DD) of that dataset"
    )


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

    # Normalized nutrition facts (per 100g or 100mL).
    #
    # This is the complete US Nutrition Facts panel — what a consumer expects to
    # find on a label — rather than an arbitrary subset. Units are OURS, not the
    # source's: sodium is always mg whether it came from USDA (mg) or Open Food
    # Facts (grams), so the same field never changes unit with its provenance.
    calories_kcal: float | None = None
    protein: NutrientValue | None = None
    fat: NutrientValue | None = None
    saturated_fat: NutrientValue | None = None
    trans_fat: NutrientValue | None = None
    cholesterol: NutrientValue | None = None
    carbohydrates: NutrientValue | None = None
    fiber: NutrientValue | None = None
    sugars: NutrientValue | None = None
    added_sugars: NutrientValue | None = None
    sodium: NutrientValue | None = None
    potassium: NutrientValue | None = None
    calcium: NutrientValue | None = None
    iron: NutrientValue | None = None
    vitamin_d: NutrientValue | None = None

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
        description=(
            "Milliseconds each upstream took on the fetch that produced this "
            "data. On a cached response these are the timings of that original "
            "fetch, not of the request you just made — see `cached`."
        ),
    )
    cached: bool = Field(
        default=False,
        description=(
            "True when this response was served from the in-memory cache "
            "without contacting any upstream."
        ),
    )
    provenance: dict[str, SourceProvenance] = Field(
        default_factory=dict,
        description=(
            "Per source, whether the data came from the local bulk copy or the "
            "live upstream API — and, for a local copy, the date of the dataset "
            "it was built from."
        ),
    )
    attribution: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Source, licence and attribution for each contributing source. "
            "Open Food Facts data is ODbL 1.0 (images CC BY-SA 3.0) and its "
            "licence *requires* attribution, so the notice travels with the "
            "data rather than living only in the documentation."
        ),
    )
