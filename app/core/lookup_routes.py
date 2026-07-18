"""
Unified food lookup API route.

The core endpoint of the Nutrition API — looks up a product by GTIN/UPC
and returns a merged CanonicalProduct from all available data sources.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException, Path, Query
from ..core.models import CanonicalProduct
from ..core import orchestrator

router = APIRouter(prefix="/api/v1", tags=["Lookup"])

# GTIN-8 (EAN-8), GTIN-12 (UPC-A), GTIN-13 (EAN-13), or GTIN-14
GTIN_PATTERN = r"^(\d{8}|\d{12,14})$"


@router.get(
    "/lookup/{gtin}",
    response_model=CanonicalProduct,
    summary="Unified food product lookup by GTIN/UPC",
)
async def lookup_product(
    gtin: str = Path(
        pattern=GTIN_PATTERN,
        description="Numeric GTIN-8, GTIN-12, GTIN-13, or GTIN-14 barcode",
    ),
    fresh: bool = Query(
        False,
        description=(
            "Bypass every cache — the in-memory cache, the local bulk copies, "
            "and the disk store — and query the live upstream APIs. Slower, and "
            "spends rate-limit budget, but returns the newest data the upstreams "
            "have. The result refreshes the caches for subsequent requests."
        ),
    ),
    sources: str = Query(
        "both",
        pattern="^(both|fdc|off)$",
        description=(
            "Scope which upstreams participate: both (default), fdc "
            "(USDA FoodData Central only), or off (Open Food Facts only). "
            "An excluded source is skipped entirely — no local-mirror read, "
            "no live-API call, no GPC matching that depends on it — not "
            "merely filtered out afterward. Only a `both` lookup uses the "
            "in-memory cache; a scoped lookup always re-fetches."
        ),
    ),
):
    """Look up a food product by its GTIN/UPC barcode.

    Concurrently queries USDA FoodData Central, Open Food Facts, and
    GS1 GPC, then merges the results into a single canonical response.

    **Reconciliation rules:**
    - USDA nutrition data overrides Open Food Facts when both are available
    - Open Food Facts provides images, ingredients, allergens, and labels
    - GS1 GPC provides the standardized category taxonomy
    - The `data_sources` field shows which sources contributed
    - The `provenance` field shows, per source, whether the data came from the
      local bulk copy (with its dataset date) or the live API
    - The `upstream_latency_ms` field shows per-source response times
    """
    product = await orchestrator.lookup(gtin, fresh=fresh, sources=sources)

    if not product.data_sources:
        raise HTTPException(
            404,
            detail=f"No data found for GTIN {gtin} in any source",
        )

    return product
