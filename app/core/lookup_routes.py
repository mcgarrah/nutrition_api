"""
Unified food lookup API route.

The core endpoint of the Nutrition API — looks up a product by GTIN/UPC
and returns a merged CanonicalProduct from all available data sources.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException
from ..core.models import CanonicalProduct
from ..core.orchestrator import lookup

router = APIRouter(prefix="/api/v1", tags=["Lookup"])


@router.get(
    "/lookup/{gtin}",
    response_model=CanonicalProduct,
    summary="Unified food product lookup by GTIN/UPC",
)
async def lookup_product(gtin: str):
    """Look up a food product by its GTIN/UPC barcode.

    Concurrently queries USDA FoodData Central, Open Food Facts, and
    GS1 GPC, then merges the results into a single canonical response.

    **Reconciliation rules:**
    - USDA nutrition data overrides Open Food Facts when both are available
    - Open Food Facts provides images, ingredients, allergens, and labels
    - GS1 GPC provides the standardized category taxonomy
    - The `data_sources` field shows which sources contributed
    - The `upstream_latency_ms` field shows per-source response times
    """
    product = await lookup(gtin)

    if not product.data_sources:
        raise HTTPException(
            404,
            detail=f"No data found for GTIN {gtin} in any source",
        )

    return product
