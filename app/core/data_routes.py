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

from . import data_browser

router = APIRouter(prefix="/api/v1/data", tags=["Data Browser"])


@router.get("/stores", summary="List the local data stores")
async def list_stores():
    """Every store the browser can open, with table names and row counts."""
    return {"stores": data_browser.list_stores()}


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
