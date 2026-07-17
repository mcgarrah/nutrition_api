"""
USDA FoodData Central API routes.

Provides endpoints for searching and retrieving USDA nutritional data.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import logging

from fastapi import APIRouter, HTTPException, Query

from . import ratelimit
from ..core import usda_fdc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/usda", tags=["USDA FDC"])


@router.get("/search", summary="Search USDA FDC foods")
async def usda_search(
    q: str = Query(..., max_length=200, description="Search query (food name, brand, etc.)"),
    page_size: int = Query(25, ge=1, le=200, description="Results per page"),
):
    """Search the USDA FoodData Central database by keyword."""
    try:
        result = await usda_fdc.search(q, page_size=page_size)
    except ratelimit.RateLimitedError as e:
        # We refused our own call to stay inside the upstream's published
        # limit. That is the caller's problem to pace, not an upstream fault.
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(max(1, int(e.retry_after) + 1))},
        )
    except Exception as e:
        logger.warning("USDA search failed for %r: %s", q, e)
        raise HTTPException(502, "USDA FDC upstream error")
    if result is None:
        raise HTTPException(
            503, "USDA FDC service unavailable (API key not configured)"
        )
    return result


@router.get("/food/{fdc_id}", summary="Get USDA FDC food by ID")
async def usda_food(fdc_id: int):
    """Get detailed nutritional data for a specific food by its FDC ID."""
    # "Not configured" and "no such food" both used to arrive as None, so a
    # missing food was reported as 503 Service Unavailable — blaming the
    # service for a question with no answer. Settle the configuration question
    # first, and None afterwards can only mean the food does not exist.
    if not usda_fdc.is_available():
        raise HTTPException(
            503, "USDA FDC service unavailable (API key not configured)"
        )

    try:
        result = await usda_fdc.get_food(fdc_id)
    except ratelimit.RateLimitedError as e:
        # We refused our own call to stay inside the upstream's published
        # limit. That is the caller's problem to pace, not an upstream fault.
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(max(1, int(e.retry_after) + 1))},
        )
    except Exception as e:
        logger.warning("USDA get_food(%s) failed: %s", fdc_id, e)
        raise HTTPException(502, "USDA FDC upstream error")

    if result is None:
        raise HTTPException(404, f"No USDA food with id {fdc_id}")
    return result


@router.get("/lookup/{upc}", summary="Look up food by UPC/GTIN barcode")
async def usda_lookup_by_upc(upc: str):
    """Look up a food product by its UPC/GTIN barcode via USDA FDC.

    Searches Branded Foods in the USDA database and returns the first match
    with full nutritional details.
    """
    try:
        result = await usda_fdc.search_by_upc(upc)
    except ratelimit.RateLimitedError as e:
        # We refused our own call to stay inside the upstream's published
        # limit. That is the caller's problem to pace, not an upstream fault.
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(max(1, int(e.retry_after) + 1))},
        )
    except Exception as e:
        logger.warning("USDA UPC lookup failed for %s: %s", upc, e)
        raise HTTPException(502, "USDA FDC upstream error")
    if result is None:
        raise HTTPException(404, "No USDA data found for this UPC/GTIN")
    return result
