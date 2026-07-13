"""
USDA FoodData Central service wrapper.

Wraps the synchronous usda_fdc.FdcClient for async use in FastAPI
by running blocking calls in a thread pool via asyncio.run_in_executor.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
from typing import Any

from dotenv import load_dotenv

from . import resilience

load_dotenv()

logger = logging.getLogger(__name__)

# Threads dedicated to USDA FDC, so a stall here cannot starve Open Food Facts
_executor = resilience.make_executor("usda")

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
    """Run a blocking USDA call on this source's own thread pool.

    Not the default executor: a stalled USDA would hold threads that OFF then
    could not get. See resilience.make_executor.
    """
    return await resilience.run_in_executor(_executor, func, *args, **kwargs)


def is_available() -> bool:
    """Check if the USDA FDC client is configured and available."""
    return _get_fdc_client() is not None


async def search(query: str, page_size: int = 25) -> dict | None:
    """Search USDA FDC for foods matching a query string.

    Returns the raw SearchResult as a dict, or None if no client is
    configured. Upstream API errors propagate to the caller.
    """
    client = _get_fdc_client()
    if not client:
        return None
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


async def get_food(fdc_id: int | str) -> dict | None:
    """Get detailed food data by FDC ID.

    Returns a dict with description, nutrients, brand info, etc., or None.
    """
    client = _get_fdc_client()
    if not client:
        return None
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


def normalize_gtin(gtin: str) -> str:
    """Normalize a barcode to GTIN-14 for comparison.

    GTIN-8/12/13/14 are the same identifier at different zero-paddings, and
    FDC is inconsistent about which it stores ("028400642255" vs
    "0099447210127"). Left-padding both sides to 14 digits makes padding
    variants compare equal. Returns "" if there are no digits, or if the
    barcode is longer than a GTIN-14.
    """
    digits = "".join(c for c in str(gtin) if c.isdigit())
    if not digits or len(digits) > 14:
        return ""
    return digits.zfill(14)


async def search_by_upc(upc: str) -> dict | None:
    """Search USDA FDC for a branded food by UPC/GTIN barcode.

    FDC exposes no barcode-lookup endpoint, so this queries the full-text
    search API. That search is fuzzy: an unknown barcode happily returns
    unrelated products (querying "00000000" yields a food whose real barcode
    is "0099447210127"). Taking the top hit on trust would attribute one
    product's nutrition to a different product's barcode — silent wrong data.

    So we verify each candidate's own gtinUpc against the requested barcode
    and return only a genuine match. We read the raw search payload because
    the usda_fdc models drop gtinUpc entirely.

    Returns the matching food's full details, or None if nothing matches /
    no client is configured. Upstream API errors propagate to the caller.
    """
    client = _get_fdc_client()
    if not client:
        return None

    target = normalize_gtin(upc)
    if not target:
        return None

    raw = await _run_sync(
        client._make_request,
        "foods/search",
        params={"query": upc, "dataType": ["Branded"], "pageSize": 10},
    )

    for food in raw.get("foods", []):
        if normalize_gtin(food.get("gtinUpc", "")) == target:
            return await get_food(food["fdcId"])

    logger.info("USDA FDC has no branded food matching GTIN %s", upc)
    return None


async def check_connectivity() -> dict:
    """Check if the USDA FDC API is reachable. For the health endpoint.

    Bounded by the upstream timeout — see the note in open_food_facts: an
    unbounded probe lets a stalled upstream hang /health, which gets the
    container restarted rather than reported as degraded.
    """
    if not is_available():
        return {"status": "unconfigured", "detail": "FDC_API_KEY not set"}
    timeout = resilience.UPSTREAM_TIMEOUT_S
    try:
        result = await asyncio.wait_for(
            _run_sync(_get_fdc_client().search, "test", page_size=1),
            timeout,
        )
        return {"status": "ok", "total_foods": result.total_hits}
    except asyncio.TimeoutError:
        return {"status": "error", "detail": f"timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
