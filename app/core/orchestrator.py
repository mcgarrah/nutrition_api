"""
DataOrchestrator — merges data from USDA FDC, Open Food Facts, and GS1 GPC
into a single CanonicalProduct response.

Reconciliation logic (layered approach from design doc):
  Layer 1: Open Food Facts — name, brand, image, ingredients, allergens, labels,
           and provisional nutrition (used only if USDA is missing).
  Layer 2: USDA FDC — authoritative nutrition data overrides OFF values.
           Product name from USDA overrides OFF if available.
  Layer 3: GS1 GPC — category taxonomy. OFF categories used as fallback.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
import time

from .models import CanonicalProduct, NutrientValue
from . import usda_fdc
from . import open_food_facts as off
from ..database import get_db

logger = logging.getLogger(__name__)


async def _fetch_off(barcode: str) -> tuple[dict | None, float]:
    """Fetch from Open Food Facts, returning (data, latency_ms)."""
    start = time.monotonic()
    try:
        data = await off.get_product(barcode)
    except Exception as e:
        logger.warning("OFF fetch failed for %s: %s", barcode, e)
        data = None
    elapsed = (time.monotonic() - start) * 1000
    return data, elapsed


async def _fetch_usda(barcode: str) -> tuple[dict | None, float]:
    """Fetch from USDA FDC by UPC, returning (data, latency_ms)."""
    start = time.monotonic()
    try:
        data = await usda_fdc.search_by_upc(barcode)
    except Exception as e:
        logger.warning("USDA fetch failed for %s: %s", barcode, e)
        data = None
    elapsed = (time.monotonic() - start) * 1000
    return data, elapsed


async def _fetch_gpc_categories(off_categories: list[str]) -> tuple[list[str], float]:
    """Try to map OFF category tags to GPC hierarchy via search.

    This is a best-effort mapping — OFF categories are informal tags,
    not GPC codes. We search the GPC bricks for matching terms.
    Returns (category_list, latency_ms).
    """
    start = time.monotonic()
    hierarchy = []
    if not off_categories:
        return hierarchy, (time.monotonic() - start) * 1000

    try:
        db = await get_db()
        # Use the first few OFF category tags as search terms
        for tag in off_categories[:3]:
            # OFF tags look like "en:beverages" — extract the label
            label = tag.split(":")[-1].replace("-", " ") if ":" in tag else tag
            rows = await db.execute_fetchall(
                """SELECT b.brick_code, b.description, b.class_code,
                          c.description AS cls_desc, c.family_code,
                          f.description AS fam_desc, f.segment_code,
                          s.description AS seg_desc
                   FROM bricks b
                   LEFT JOIN classes c ON b.class_code = c.class_code
                   LEFT JOIN families f ON c.family_code = f.family_code
                   LEFT JOIN segments s ON f.segment_code = s.segment_code
                   WHERE b.description LIKE ?
                   LIMIT 1""",
                [f"%{label}%"],
            )
            if rows:
                r = rows[0]
                parts = [p for p in [r[7], r[5], r[3], r[1]] if p]
                if parts:
                    hierarchy = parts
                    break
    except Exception as e:
        logger.warning("GPC category lookup failed: %s", e)

    elapsed = (time.monotonic() - start) * 1000
    return hierarchy, elapsed


def _nv(value, unit="g") -> NutrientValue | None:
    """Create a NutrientValue if value is not None."""
    if value is not None:
        return NutrientValue(value=float(value), unit=unit)
    return None


def _usda_nutrient(nutrients: dict, name: str) -> float | None:
    """Extract a nutrient amount from USDA nutrients dict by name."""
    entry = nutrients.get(name)
    if entry and entry.get("amount") is not None:
        return entry["amount"]
    return None


async def lookup(gtin: str) -> CanonicalProduct:
    """Look up a product by GTIN/UPC and merge data from all sources.

    Fires OFF and USDA queries in parallel, then layers the results.
    """
    # Parallel fetch from OFF and USDA
    (off_data, off_ms), (usda_data, usda_ms) = await asyncio.gather(
        _fetch_off(gtin),
        _fetch_usda(gtin),
    )

    product = CanonicalProduct(gtin=gtin)
    product.upstream_latency_ms["OpenFoodFacts"] = round(off_ms, 1)
    product.upstream_latency_ms["USDA_FDC"] = round(usda_ms, 1)

    # --- Layer 1: Open Food Facts (name, image, ingredients, provisional nutrition) ---
    if off_data:
        product.data_sources.append("OpenFoodFacts")
        product.product_name = off_data.get("product_name") or product.product_name
        product.brand = off_data.get("brands") or product.brand
        product.image_url = off_data.get("image_url")
        product.ingredients_text = off_data.get("ingredients_text")
        product.allergens = off_data.get("allergens", [])
        product.labels = off_data.get("labels", [])

        # Provisional nutrition from OFF (per 100g)
        nutr = off_data.get("nutrients_per_100g", {})
        product.calories_kcal = nutr.get("calories_kcal")
        product.protein = _nv(nutr.get("protein_g"))
        product.fat = _nv(nutr.get("fat_g"))
        product.carbohydrates = _nv(nutr.get("carbohydrates_g"))
        product.fiber = _nv(nutr.get("fiber_g"))
        product.sugars = _nv(nutr.get("sugars_g"))
        product.sodium = _nv(nutr.get("sodium_g"))

    # --- Layer 2: USDA FDC (authoritative nutrition overrides OFF) ---
    if usda_data:
        product.data_sources.append("USDA_FDC")
        # USDA name overrides OFF if available
        usda_desc = usda_data.get("description")
        if usda_desc:
            product.product_name = usda_desc
        # USDA brand overrides OFF
        usda_brand = usda_data.get("brand_owner") or usda_data.get("brand_name")
        if usda_brand:
            product.brand = usda_brand

        nutrients = usda_data.get("nutrients", {})
        if nutrients:
            # Override nutrition with USDA values (authoritative)
            energy = _usda_nutrient(nutrients, "Energy")
            if energy is not None:
                product.calories_kcal = energy
            protein = _usda_nutrient(nutrients, "Protein")
            if protein is not None:
                product.protein = _nv(protein)
            fat = _usda_nutrient(nutrients, "Total lipid (fat)")
            if fat is not None:
                product.fat = _nv(fat)
            carbs = _usda_nutrient(nutrients, "Carbohydrate, by difference")
            if carbs is not None:
                product.carbohydrates = _nv(carbs)
            fiber = _usda_nutrient(nutrients, "Fiber, total dietary")
            if fiber is not None:
                product.fiber = _nv(fiber)
            sugars = _usda_nutrient(nutrients, "Sugars, total including NLEA")
            if sugars is not None:
                product.sugars = _nv(sugars)
            sodium = _usda_nutrient(nutrients, "Sodium, Na")
            if sodium is not None:
                product.sodium = _nv(sodium, unit="mg")

        # Use USDA ingredients if OFF didn't have them
        if not product.ingredients_text and usda_data.get("ingredients"):
            product.ingredients_text = usda_data["ingredients"]

    # --- Layer 3: GS1 GPC (category taxonomy) ---
    off_categories = off_data.get("categories", []) if off_data else []
    gpc_hierarchy, gpc_ms = await _fetch_gpc_categories(off_categories)
    product.upstream_latency_ms["GS1_GPC"] = round(gpc_ms, 1)
    if gpc_hierarchy:
        product.category_hierarchy = gpc_hierarchy
        product.data_sources.append("GS1_GPC")
    elif off_categories:
        # Fallback: use OFF category tags as-is
        product.category_hierarchy = [
            tag.split(":")[-1].replace("-", " ").title() if ":" in tag else tag
            for tag in off_categories[:5]
        ]

    return product
