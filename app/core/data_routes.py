"""
Read-only Data Browser API.

Exposes the local data stores — Open Food Facts, USDA FDC, GS1 GPC, and the
response store — for inspection: their schema, their rows (paginated and
searchable), and per-column coverage. Everything is read-only; see
app/core/data_browser.py for the safety model.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException, Query

from . import analytics
from . import data_browser

router = APIRouter(prefix="/api/v1/data", tags=["Data Browser"])


@router.get("/stores", summary="List the local data stores")
async def list_stores():
    """Every store the browser can open, with table names and row counts."""
    return {"stores": data_browser.list_stores()}


@router.get("/analytics", summary="Data quality & coverage dashboard summary")
async def analytics_summary():
    """One aggregated payload for the /data/analytics dashboard: dataset
    provenance for each local source, per-nutrient FDC-vs-OFF coverage, and
    GPC category-matching coverage (fdc_curated and reviewed). See
    app/core/analytics.py and PLAN.md item 6.

    JSON-first: the intended primary consumer is a script or notebook, not
    just the dashboard page built on top of it.
    """
    return await analytics.summary()


@router.get("/{store_id}/schema", summary="Schema of one store")
async def store_schema(store_id: str):
    result = data_browser.schema(store_id)
    if result is None:
        raise HTTPException(404, f"No store '{store_id}' available")
    return {"store": store_id, "tables": result}


@router.get("/{store_id}/rows", summary="Browse rows of a table")
async def store_rows(
    store_id: str,
    table: str = Query(..., description="Table (or response-store namespace) to browse"),
    limit: int = Query(50, ge=1, le=data_browser.MAX_ROWS),
    offset: int = Query(0, ge=0),
    q: str = Query("", description="Substring match across the text columns"),
    sort: str = Query("", description="Column to sort by (must be a real column)"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    result = data_browser.rows(store_id, table, limit, offset, q, sort, dir)
    if result is None:
        raise HTTPException(404, f"No table '{table}' in store '{store_id}'")
    return result


@router.get("/{store_id}/coverage", summary="Per-column non-null coverage")
async def store_coverage(
    store_id: str,
    table: str = Query(..., description="Table to summarise"),
):
    """Non-null percentage per column — a quick read on sparsity and dead
    columns (Open Food Facts' allergens column, for instance, is 0%)."""
    result = data_browser.coverage(store_id, table)
    if result is None:
        raise HTTPException(404, f"No table '{table}' in store '{store_id}'")
    return result


@router.get("/{store_id}/numeric-columns", summary="Columns worth histogramming")
async def store_numeric_columns(
    store_id: str,
    table: str = Query(..., description="Table to inspect"),
):
    """The REAL/INTEGER-typed columns of a table -- what a client should
    offer as choices before calling .../histogram, rather than guessing."""
    result = data_browser.numeric_columns(store_id, table)
    if result is None:
        raise HTTPException(404, f"No table '{table}' in store '{store_id}'")
    return {"store": store_id, "table": table, "columns": result}


@router.get("/{store_id}/histogram", summary="Value distribution for one numeric column")
async def store_histogram(
    store_id: str,
    table: str = Query(..., description="Table to summarise"),
    column: str = Query(..., description="Numeric column to bucket"),
    bins: int = Query(20, ge=1, le=50),
):
    """Bucketed value distribution, binned over the 1st-99th percentile
    range so a single garbage outlier can't collapse the whole histogram
    into one bucket -- `below_range`/`above_range` report what got clipped
    rather than hiding it. Catches what a pure non-null coverage check
    can't: a column that's well-populated but suspiciously clustered (e.g.
    values bunched at the physical-max sanity bound app/core/nutrients.py
    enforces, suggesting capped rather than reported data)."""
    result = data_browser.histogram(store_id, table, column, bins)
    if result is None:
        raise HTTPException(404, f"No column '{column}' in table '{table}' of store '{store_id}'")
    return result


@router.get("/{store_id}/record", summary="A full response-store record")
async def store_record(
    store_id: str,
    table: str = Query(..., description="Response-store namespace"),
    key: str = Query(..., description="Record key"),
):
    result = data_browser.record(store_id, table, key)
    if result is None:
        raise HTTPException(404, "Record not found")
    return result
