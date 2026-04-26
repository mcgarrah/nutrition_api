"""
USDA FoodData Central service wrapper.

Wraps the synchronous usda_fdc.FdcClient for async use in FastAPI
by running blocking calls in a thread pool via asyncio.run_in_executor.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
import os
from functools import partial
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Lazy-initialized singleton client
_fdc_client = None
_fdc_available = None  # None = not checked, True/False = checked


def _get_fdc_client():
    """Return the FdcClient singleton, or None if no API key is configured."""
    global _fdc_client, _fdc_available
    if _fdc_available is False:
        return None
    if _fdc_client is not None:
        return _fdc_client
    try:
        from usda_fdc import FdcClient
        _fdc_client = FdcClient()  # reads FDC_API_KEY from env
        _fdc_available = True
        logger.info("USDA FDC client initialized (API key configured).")
        return _fdc_client
    except (ValueError, ImportError) as e:
        _fdc_available = False
        logger.warning("USDA FDC client unavailable: %s", e)
        return None


async def _run_sync(func, *args, **kwargs) -> Any:
    """Run a synchronous function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def is_available() -> bool:
    """Check if the USDA FDC client is configured and available."""
    return _get_fdc_client() is not None


async def search(query: str, page_size: int = 25) -> dict | None:
    """Search USDA FDC for foods matching a query string.

    Returns the raw SearchResult as a dict, or None if unavailable.
    """
    client = _get_fdc_client()
    if not client:
        return None
    try:
        result = await _run_sync(client.search, query, page_size=page_size)
        return {
            "total_hits": result.total_hits,
            "foods": [
                {
                    "fdc_id": f.fdc_id,
                    "description": f.description,
                    "data_type": f.data_type,
                    "brand_owner": f.brand_owner,
                    "brand_name": f.brand_name,
                }
                for f in result.foods
            ],
        }
    except Exception as e:
        logger.warning("USDA FDC search failed: %s", e)
        return None


async def get_food(fdc_id: int | str) -> dict | None:
    """Get detailed food data by FDC ID.

    Returns a dict with description, nutrients, brand info, etc., or None.
    """
    client = _get_fdc_client()
    if not client:
        return None
    try:
        food = await _run_sync(client.get_food, fdc_id)
        return {
            "fdc_id": food.fdc_id,
            "description": food.description,
            "data_type": food.data_type,
            "brand_owner": food.brand_owner,
            "brand_name": food.brand_name,
            "ingredients": food.ingredients,
            "serving_size": food.serving_size,
            "serving_size_unit": food.serving_size_unit,
            "nutrients": {
                n.name: {"amount": n.amount, "unit": n.unit_name}
                for n in food.nutrients
            },
        }
    except Exception as e:
        logger.warning("USDA FDC get_food(%s) failed: %s", fdc_id, e)
        return None


async def search_by_upc(upc: str) -> dict | None:
    """Search USDA FDC for a food by UPC/GTIN barcode.

    Uses the FDC search API with the UPC as the query, filtered to Branded foods.
    Returns the first matching food's full details, or None.
    """
    client = _get_fdc_client()
    if not client:
        return None
    try:
        result = await _run_sync(
            client.search, upc, data_type=["Branded"], page_size=5,
        )
        if not result.foods:
            return None
        # Get full details for the first match
        return await get_food(result.foods[0].fdc_id)
    except Exception as e:
        logger.warning("USDA FDC search_by_upc(%s) failed: %s", upc, e)
        return None


async def check_connectivity() -> dict:
    """Check if the USDA FDC API is reachable. For health endpoint."""
    if not is_available():
        return {"status": "unconfigured", "detail": "FDC_API_KEY not set"}
    try:
        result = await _run_sync(
            _get_fdc_client().search, "test", page_size=1,
        )
        return {"status": "ok", "total_foods": result.total_hits}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
