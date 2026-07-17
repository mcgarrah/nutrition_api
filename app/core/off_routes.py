"""
Open Food Facts API routes.

Provides endpoints for searching and retrieving product data from
the Open Food Facts crowdsourced database.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import logging

from fastapi import APIRouter, HTTPException, Query

from . import ratelimit
from ..core import open_food_facts as off

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/off", tags=["Open Food Facts"])


@router.get("/product/{barcode}", summary="Get product by barcode")
async def off_product(barcode: str):
    """Look up a product by its barcode (UPC/EAN/GTIN) in Open Food Facts.

    Returns product name, brand, image, ingredients, and per-100g nutrients.
    """
    try:
        result = await off.get_product(barcode)
    except ratelimit.RateLimitedError as e:
        # We refused our own call to stay inside the upstream's published
        # limit. That is the caller's problem to pace, not an upstream fault.
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(max(1, int(e.retry_after) + 1))},
        )
    except Exception as e:
        logger.warning("OFF product lookup failed for %s: %s", barcode, e)
        raise HTTPException(502, "Open Food Facts upstream error")
    if result is None:
        raise HTTPException(404, "Product not found in Open Food Facts")
    return result


@router.get("/search", summary="Search Open Food Facts")
async def off_search(
    q: str = Query(..., max_length=200, description="Search query"),
    page_size: int = Query(25, ge=1, le=100, description="Results per page"),
):
    """Search the Open Food Facts database by keyword."""
    try:
        result = await off.search(q, page_size=page_size)
    except ratelimit.RateLimitedError as e:
        # We refused our own call to stay inside the upstream's published
        # limit. That is the caller's problem to pace, not an upstream fault.
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(max(1, int(e.retry_after) + 1))},
        )
    except Exception as e:
        logger.warning("OFF search failed for %r: %s", q, e)
        raise HTTPException(502, "Open Food Facts upstream error")
    if result is None:
        raise HTTPException(503, "Open Food Facts service unavailable")
    return result
