"""
Canonical data models for the unified food intelligence API.

These models represent the single source of truth returned by the
/api/v1/lookup/{gtin} endpoint, abstracting away the differences
between USDA FDC, Open Food Facts, and GS1 GPC data sources.

All nutrient values are normalized to per-100g basis.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from typing import Literal

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
    category_hierarchy_source: Literal["fdc_curated", "off_fuzzy", "none"] = Field(
        default="none",
        description=(
            "How category_hierarchy was produced, so a caller can tell a "
            "verified classification from a best-effort guess:\n"
            "'fdc_curated' — FDC's own branded_food_category, resolved through "
            "a hand-verified table (app/core/gpc_match.py). High confidence.\n"
            "'off_fuzzy' — Open Food Facts category tags, resolved by "
            "best-effort text matching against GPC brick descriptions. "
            "Real matches, but not verified case by case — treat as a hint, "
            "not ground truth.\n"
            "'none' — no source produced a GPC-verified category. "
            "category_hierarchy may still be populated with raw OFF tags as "
            "a fallback in that case; those are upstream labels, not a GPC "
            "classification.\n"
            "A future 'reviewed' tier is planned for off_fuzzy matches that "
            "have been human-checked, once that review process exists — not "
            "implemented yet."
        ),
    )

    # Normalized nutrition facts (per 100g or 100mL).
    #
    # This is the US Nutrition Facts panel plus every vitamin and mineral
    # tracked in app/core/nutrients.py — a label's macros, and the
    # micronutrient panel beyond it. Individual amino acids and sugar-type
    # breakdowns are deliberately out of scope; see that module's docstring.
    # Units are OURS, not the source's: sodium is always mg whether it came
    # from USDA (mg) or Open Food Facts (grams), so the same field never
    # changes unit with its provenance.
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

    # Vitamins beyond the mandatory label panel.
    vitamin_a: NutrientValue | None = None
    vitamin_c: NutrientValue | None = None
    vitamin_e: NutrientValue | None = None
    vitamin_k: NutrientValue | None = None
    thiamin: NutrientValue | None = Field(default=None, description="Vitamin B1")
    riboflavin: NutrientValue | None = Field(default=None, description="Vitamin B2")
    niacin: NutrientValue | None = Field(default=None, description="Vitamin B3")
    vitamin_b6: NutrientValue | None = None
    folate: NutrientValue | None = Field(default=None, description="Vitamin B9")
    vitamin_b12: NutrientValue | None = None
    pantothenic_acid: NutrientValue | None = Field(default=None, description="Vitamin B5")
    biotin: NutrientValue | None = Field(default=None, description="Vitamin B7")
    choline: NutrientValue | None = None

    # Minerals beyond the mandatory label panel.
    magnesium: NutrientValue | None = None
    zinc: NutrientValue | None = None
    phosphorus: NutrientValue | None = None
    selenium: NutrientValue | None = None
    copper: NutrientValue | None = None
    manganese: NutrientValue | None = None
    molybdenum: NutrientValue | None = None

    caffeine: NutrientValue | None = None

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
