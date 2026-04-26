"""
USDA FoodData Central API routes.

Provides endpoints for searching and retrieving USDA nutritional data.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException, Query
from ..core import usda_fdc

router = APIRouter(prefix="/api/v1/usda", tags=["USDA FDC"])


@router.get("/search", summary="Search USDA FDC foods")
async def usda_search(
    q: str = Query(..., description="Search query (food name, brand, etc.)"),
    page_size: int = Query(25, ge=1, le=200, description="Results per page"),
):
    """Search the USDA FoodData Central database by keyword."""
    result = await usda_fdc.search(q, page_size=page_size)
    if result is None:
        raise HTTPException(503, "USDA FDC service unavailable (API key not configured or API error)")
    return result


@router.get("/food/{fdc_id}", summary="Get USDA FDC food by ID")
async def usda_food(fdc_id: int):
    """Get detailed nutritional data for a specific food by its FDC ID."""
    result = await usda_fdc.get_food(fdc_id)
    if result is None:
        raise HTTPException(404, "Food not found or USDA FDC service unavailable")
    return result


@router.get("/lookup/{upc}", summary="Look up food by UPC/GTIN barcode")
async def usda_lookup_by_upc(upc: str):
    """Look up a food product by its UPC/GTIN barcode via USDA FDC.

    Searches Branded Foods in the USDA database and returns the first match
    with full nutritional details.
    """
    result = await usda_fdc.search_by_upc(upc)
    if result is None:
        raise HTTPException(404, "No USDA data found for this UPC/GTIN")
    return result
