"""
Product name search API route.

Backs the search UI (/search): a caller types a product name, gets a list of
candidates from the local FDC/OFF copies, and picks one to look up in full via
the existing GET /api/v1/lookup/{gtin}. See app/core/search.py for why this is
local-DB-backed rather than a live upstream text search.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, Query

from . import search
from .models import SearchResponse, SearchResult

router = APIRouter(prefix="/api/v1", tags=["Search"])


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search local product copies by name",
)
async def search_by_name(
    q: str = Query("", description="Product name (or a substring of it) to search for"),
    limit: int = Query(
        search.DEFAULT_RESULTS, ge=1, le=search.MAX_RESULTS,
        description="Maximum results to return",
    ),
):
    """Search the local FDC and OFF bulk copies by product name.

    Returns identity fields only (barcode, name, brand, image) — enough to
    render a result list. Look up a chosen result's full nutrition panel via
    GET /api/v1/lookup/{gtin}, which merges FDC, OFF, and GPC exactly as it
    already does for a direct barcode lookup.

    An empty `results` list means neither local copy's product name matched —
    not that no upstream has it. Local copies are only as fresh as their last
    import, and this endpoint does not fall through to a live search.
    """
    results = search.search_products(q, limit)
    return SearchResponse(
        query=q,
        results=[SearchResult(**r) for r in results],
    )
