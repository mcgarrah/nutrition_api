"""
Open Food Facts service wrapper.

Wraps the synchronous openfoodfacts SDK for async use in FastAPI
by running blocking calls in a thread pool via asyncio.run_in_executor.

No API key required — OFF is open data. A user_agent is required by their TOS.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

# Fields we request from OFF to minimize payload
_OFF_FIELDS = [
    "product_name", "brands", "image_url", "ingredients_text",
    "nutriments", "categories_tags", "code", "quantity",
    "serving_size", "allergens_tags", "labels_tags",
]

# Lazy-initialized singleton
_off_api = None


def _get_off_api():
    """Return the OFF API singleton."""
    global _off_api
    if _off_api is not None:
        return _off_api
    try:
        import openfoodfacts
        _off_api = openfoodfacts.API(
            user_agent="NutritionAPI/0.1 (mcgarrah@gmail.com)",
        )
        logger.info("Open Food Facts client initialized.")
        return _off_api
    except ImportError as e:
        logger.warning("openfoodfacts library not installed: %s", e)
        return None


async def _run_sync(func, *args, **kwargs) -> Any:
    """Run a synchronous function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def _extract_nutrients(nutriments: dict) -> dict:
    """Extract key nutrients from OFF nutriments dict (per 100g values)."""
    keys = {
        "energy-kcal_100g": "calories_kcal",
        "proteins_100g": "protein_g",
        "fat_100g": "fat_g",
        "carbohydrates_100g": "carbohydrates_g",
        "fiber_100g": "fiber_g",
        "sugars_100g": "sugars_g",
        "sodium_100g": "sodium_g",
        "salt_100g": "salt_g",
    }
    result = {}
    for off_key, our_key in keys.items():
        val = nutriments.get(off_key)
        if val is not None:
            result[our_key] = val
    return result


def _format_product(data: dict) -> dict:
    """Format an OFF product response into our standard shape."""
    nutriments = data.get("nutriments", {})
    return {
        "barcode": data.get("code"),
        "product_name": data.get("product_name"),
        "brands": data.get("brands"),
        "image_url": data.get("image_url"),
        "ingredients_text": data.get("ingredients_text"),
        "quantity": data.get("quantity"),
        "serving_size": data.get("serving_size"),
        "categories": data.get("categories_tags", []),
        "allergens": data.get("allergens_tags", []),
        "labels": data.get("labels_tags", []),
        "nutrients_per_100g": _extract_nutrients(nutriments),
    }


async def get_product(barcode: str) -> dict | None:
    """Look up a product by barcode (UPC/EAN/GTIN).

    Returns a formatted dict with product info and nutrients, or None.
    """
    api = _get_off_api()
    if not api:
        return None
    try:
        data = await _run_sync(
            api.product.get, barcode, fields=_OFF_FIELDS,
        )
        if not data or not data.get("product_name"):
            return None
        return _format_product(data)
    except Exception as e:
        logger.warning("OFF get_product(%s) failed: %s", barcode, e)
        return None


async def search(query: str, page_size: int = 25) -> dict | None:
    """Search OFF by text query.

    Returns a dict with total count and list of products, or None.
    """
    api = _get_off_api()
    if not api:
        return None
    try:
        data = await _run_sync(
            api.product.text_search, query, page_size=page_size,
        )
        if not data or "products" not in data:
            return None
        return {
            "total": data.get("count", 0),
            "products": [_format_product(p) for p in data["products"]],
        }
    except Exception as e:
        logger.warning("OFF search(%s) failed: %s", query, e)
        return None


async def check_connectivity() -> dict:
    """Check if the OFF API is reachable. For health endpoint."""
    try:
        # Look up a well-known product as a connectivity test
        result = await get_product("3017620422003")  # Nutella
        if result:
            return {"status": "ok"}
        return {"status": "error", "detail": "Test product not found"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
