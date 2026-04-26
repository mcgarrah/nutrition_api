"""
Open Food Facts API routes.

Provides endpoints for searching and retrieving product data from
the Open Food Facts crowdsourced database.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException, Query
from ..core import open_food_facts as off

router = APIRouter(prefix="/api/v1/off", tags=["Open Food Facts"])


@router.get("/product/{barcode}", summary="Get product by barcode")
async def off_product(barcode: str):
    """Look up a product by its barcode (UPC/EAN/GTIN) in Open Food Facts.

    Returns product name, brand, image, ingredients, and per-100g nutrients.
    """
    result = await off.get_product(barcode)
    if result is None:
        raise HTTPException(404, "Product not found in Open Food Facts")
    return result


@router.get("/search", summary="Search Open Food Facts")
async def off_search(
    q: str = Query(..., description="Search query"),
    page_size: int = Query(25, ge=1, le=100, description="Results per page"),
):
    """Search the Open Food Facts database by keyword."""
    result = await off.search(q, page_size=page_size)
    if result is None:
        raise HTTPException(503, "Open Food Facts service unavailable")
    return result
