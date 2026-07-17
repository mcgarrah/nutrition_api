"""
GS1 GPC API routes for FastAPI.

Provides the same endpoint contract as the shiny-shop Django REST Framework API,
but queries a corrected schema with junction tables for many-to-many relationships.

Endpoints:
  /api/v1/gpc/segments/              - list (paginated, searchable)
  /api/v1/gpc/segments/{code}        - detail with families
  /api/v1/gpc/families/              - list (paginated, searchable, filterable by segment)
  /api/v1/gpc/families/{code}        - detail with classes + parent breadcrumb
  /api/v1/gpc/classes/               - list (paginated, searchable, filterable by family)
  /api/v1/gpc/classes/{code}         - detail with bricks + parent breadcrumb
  /api/v1/gpc/bricks/                - list (paginated, searchable, filterable by class)
  /api/v1/gpc/bricks/{code}          - detail with attributes + parent breadcrumb
  /api/v1/gpc/search/?q=...          - cross-entity search
  /api/v1/gpc/mappings               - curated FDC-category -> GPC mappings + coverage

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from ..database import get_db
from ..core import gpc_match
from .models import (
    SegmentItem, SegmentDetail,
    FamilyItem, FamilyDetail, ParentSegmentRef,
    ClassItem, ClassDetail, ParentFamilyRef,
    BrickItem, BrickDetail, ParentClassRef,
    AttributeTypeItem, AttributeValueItem,
    AttributeMatch,
    PaginatedResponse, SearchResponse,
    CuratedMapping, MappingCoverage, MappingsResponse,
)

router = APIRouter(prefix="/api/v1/gpc", tags=["GPC"])

DEFAULT_PAGE_SIZE = 20


def _paginate_url(request: Request, page: int | None, page_size: int) -> str | None:
    """Build a next/previous link that keeps the caller's filters.

    Rebuilding the URL from the path alone drops every other query parameter,
    so following `next` on a filtered list silently returns the *unfiltered*
    page 2 — a different result set than the `count` beside it describes.
    include_query_params keeps everything and overrides only the paging keys.
    """
    if page is None:
        return None
    return str(request.url.include_query_params(page=page, page_size=page_size))


def _page_params(page: int, page_size: int, total: int):
    offset = (page - 1) * page_size
    next_page = page + 1 if offset + page_size < total else None
    prev_page = page - 1 if page > 1 else None
    return offset, next_page, prev_page


async def _count_and_fetch(table, columns, where="", params=None, order_by="",
                           page=1, page_size=DEFAULT_PAGE_SIZE):
    """Helper: count + paginated fetch for a single table."""
    db = await get_db()
    params = params or []
    row = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table} {where}", params)
    total = row[0][0]
    offset, next_page, prev_page = _page_params(page, page_size, total)
    rows = await db.execute_fetchall(
        f"SELECT {columns} FROM {table} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    return total, rows, next_page, prev_page


# ── Segments ──────────────────────────────────────────────────────────

@router.get("/segments/", response_model=PaginatedResponse, summary="List all GPC Segments")
async def list_segments(
    request: Request,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=100),
):
    where, params = "", []
    if search:
        where = "WHERE segment_code LIKE ? OR description LIKE ?"
        params = [f"%{search}%", f"%{search}%"]
    total, rows, next_page, prev_page = await _count_and_fetch(
        "segments", "segment_code, description", where, params, "segment_code", page, page_size,
    )
    return PaginatedResponse(
        count=total,
        next=_paginate_url(request, next_page, page_size),
        previous=_paginate_url(request, prev_page, page_size),
        results=[SegmentItem(segment_code=r[0], description=r[1]) for r in rows],
    )


@router.get("/segments/{segment_code}", response_model=SegmentDetail,
            summary="Retrieve a GPC Segment")
async def get_segment(segment_code: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT segment_code, description FROM segments WHERE segment_code = ?",
        [segment_code],
    )
    if not rows:
        raise HTTPException(404, "Segment not found")
    seg = rows[0]
    families = await db.execute_fetchall(
        "SELECT family_code, description FROM families WHERE segment_code = ? ORDER BY family_code",
        [segment_code],
    )
    return SegmentDetail(
        segment_code=seg[0], description=seg[1],
        families=[FamilyItem(family_code=f[0], description=f[1]) for f in families],
    )


# ── Families ──────────────────────────────────────────────────────────

@router.get("/families/", response_model=PaginatedResponse, summary="List all GPC Families")
async def list_families(
    request: Request,
    search: str | None = None,
    segment_code: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=100),
):
    clauses, params = [], []
    if search:
        clauses.append("(family_code LIKE ? OR description LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if segment_code:
        clauses.append("segment_code = ?")
        params.append(segment_code)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total, rows, next_page, prev_page = await _count_and_fetch(
        "families", "family_code, description", where, params, "family_code", page, page_size,
    )
    return PaginatedResponse(
        count=total,
        next=_paginate_url(request, next_page, page_size),
        previous=_paginate_url(request, prev_page, page_size),
        results=[FamilyItem(family_code=r[0], description=r[1]) for r in rows],
    )


@router.get("/families/{family_code}", response_model=FamilyDetail, summary="Retrieve a GPC Family")
async def get_family(family_code: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT f.family_code, f.description, f.segment_code, s.description
           FROM families f LEFT JOIN segments s ON f.segment_code = s.segment_code
           WHERE f.family_code = ?""",
        [family_code],
    )
    if not rows:
        raise HTTPException(404, "Family not found")
    r = rows[0]
    classes = await db.execute_fetchall(
        "SELECT class_code, description FROM classes WHERE family_code = ? ORDER BY class_code",
        [family_code],
    )
    seg_details = ParentSegmentRef(segment_code=r[2], segment_description=r[3]) if r[2] else None
    full_path = f"{r[3]} > {r[1]}" if r[3] else r[1]
    return FamilyDetail(
        family_code=r[0], description=r[1], segment_code=r[2],
        segment_code_details=seg_details, full_path=full_path,
        classes=[ClassItem(class_code=c[0], description=c[1]) for c in classes],
    )


# ── Classes ───────────────────────────────────────────────────────────

@router.get("/classes/", response_model=PaginatedResponse, summary="List all GPC Classes")
async def list_classes(
    request: Request,
    search: str | None = None,
    family_code: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=100),
):
    clauses, params = [], []
    if search:
        clauses.append("(class_code LIKE ? OR description LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if family_code:
        clauses.append("family_code = ?")
        params.append(family_code)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total, rows, next_page, prev_page = await _count_and_fetch(
        "classes", "class_code, description", where, params, "class_code", page, page_size,
    )
    return PaginatedResponse(
        count=total,
        next=_paginate_url(request, next_page, page_size),
        previous=_paginate_url(request, prev_page, page_size),
        results=[ClassItem(class_code=r[0], description=r[1]) for r in rows],
    )


@router.get("/classes/{class_code}", response_model=ClassDetail, summary="Retrieve a GPC Class")
async def get_class(class_code: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT c.class_code, c.description, c.family_code,
                  f.description, f.segment_code, s.description
           FROM classes c
           LEFT JOIN families f ON c.family_code = f.family_code
           LEFT JOIN segments s ON f.segment_code = s.segment_code
           WHERE c.class_code = ?""",
        [class_code],
    )
    if not rows:
        raise HTTPException(404, "Class not found")
    r = rows[0]
    bricks = await db.execute_fetchall(
        "SELECT brick_code, description FROM bricks WHERE class_code = ? ORDER BY brick_code",
        [class_code],
    )
    fam_details = None
    if r[2]:
        fam_details = ParentFamilyRef(
            family_code=r[2], description=r[3],
            segment_code=r[4], segment_description=r[5],
        )
    parts = [p for p in [r[5], r[3], r[1]] if p]
    return ClassDetail(
        class_code=r[0], description=r[1], family_code=r[2],
        family_code_details=fam_details, full_path=" > ".join(parts),
        bricks=[BrickItem(brick_code=b[0], description=b[1]) for b in bricks],
    )


# ── Bricks ────────────────────────────────────────────────────────────

@router.get("/bricks/", response_model=PaginatedResponse, summary="List all GPC Bricks")
async def list_bricks(
    request: Request,
    search: str | None = None,
    class_code: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=100),
):
    clauses, params = [], []
    if search:
        clauses.append("(brick_code LIKE ? OR description LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if class_code:
        clauses.append("class_code = ?")
        params.append(class_code)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total, rows, next_page, prev_page = await _count_and_fetch(
        "bricks", "brick_code, description", where, params, "brick_code", page, page_size,
    )
    return PaginatedResponse(
        count=total,
        next=_paginate_url(request, next_page, page_size),
        previous=_paginate_url(request, prev_page, page_size),
        results=[BrickItem(brick_code=r[0], description=r[1]) for r in rows],
    )


@router.get("/bricks/{brick_code}", response_model=BrickDetail, summary="Retrieve a GPC Brick")
async def get_brick(brick_code: str):
    db = await get_db()

    # Brick + parent hierarchy
    rows = await db.execute_fetchall(
        """SELECT b.brick_code, b.description, b.class_code,
                  c.description, c.family_code,
                  f.description, f.segment_code,
                  s.description
           FROM bricks b
           LEFT JOIN classes c ON b.class_code = c.class_code
           LEFT JOIN families f ON c.family_code = f.family_code
           LEFT JOIN segments s ON f.segment_code = s.segment_code
           WHERE b.brick_code = ?""",
        [brick_code],
    )
    if not rows:
        raise HTTPException(404, "Brick not found")
    r = rows[0]

    # Attribute types for THIS brick (via junction table)
    att_types = await db.execute_fetchall(
        """SELECT at.att_type_code, at.att_type_text
           FROM brick_attribute_types bat
           JOIN attribute_types at ON bat.att_type_code = at.att_type_code
           WHERE bat.brick_code = ?
           ORDER BY at.att_type_code""",
        [brick_code],
    )

    # Attribute values for each type (via junction table)
    attributes = []
    for at in att_types:
        vals = await db.execute_fetchall(
            """SELECT av.att_value_code, av.att_value_text
               FROM attribute_type_values atv
               JOIN attribute_values av ON atv.att_value_code = av.att_value_code
               WHERE atv.att_type_code = ?
               ORDER BY av.att_value_code""",
            [at[0]],
        )
        attributes.append(AttributeTypeItem(
            att_type_code=at[0], att_type_text=at[1],
            values=[AttributeValueItem(att_value_code=v[0], att_value_text=v[1]) for v in vals],
        ))

    cls_details = None
    if r[2]:
        cls_details = ParentClassRef(
            class_code=r[2], description=r[3],
            family_code=r[4], family_description=r[5],
            segment_code=r[6], segment_description=r[7],
        )
    parts = [p for p in [r[7], r[5], r[3], r[1]] if p]

    return BrickDetail(
        brick_code=r[0], description=r[1], class_code=r[2],
        class_code_details=cls_details, full_path=" > ".join(parts),
        attributes=attributes,
    )


# ── Search ────────────────────────────────────────────────────────────

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200

# entity -> (table, code column, item model, attribute name on SearchResponse)
_SEARCHABLE = [
    ("segments", "segments", "segment_code", SegmentItem),
    ("families", "families", "family_code", FamilyItem),
    ("classes", "classes", "class_code", ClassItem),
    ("bricks", "bricks", "brick_code", BrickItem),
]


async def _search_entity(db, table, code_column, model, like, limit):
    """Count the matches, then fetch at most `limit` of them."""
    total = (await db.execute_fetchall(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE {code_column} LIKE ? OR description LIKE ?",
        [like, like],
    ))[0][0]

    rows = await db.execute_fetchall(
        f"SELECT {code_column}, description FROM {table} "
        f"WHERE {code_column} LIKE ? OR description LIKE ? "
        f"ORDER BY {code_column} LIMIT ?",
        [like, like, limit],
    )
    items = [model(**{code_column: r[0], "description": r[1]}) for r in rows]
    return items, total


async def _search_attributes(db, like, limit):
    """Find attribute types and values matching the query, with their bricks.

    This is what makes the specific findable. GPC keeps detail in attributes,
    not brick names: "olive oil" is the value "OLIVE OIL" of an attribute on the
    generic "Oils Edible" brick. A search of brick descriptions alone returns
    nothing for it. Each match therefore carries the bricks that hold the
    attribute, so the caller can walk back into the hierarchy.
    """
    # Attribute VALUES that match, paired with their type (a value can belong to
    # more than one type, so join through attribute_type_values).
    value_rows = await db.execute_fetchall(
        """SELECT av.att_value_code, av.att_value_text,
                  at.att_type_code, at.att_type_text
           FROM attribute_values av
           JOIN attribute_type_values atv ON av.att_value_code = atv.att_value_code
           JOIN attribute_types at ON atv.att_type_code = at.att_type_code
           WHERE av.att_value_text LIKE ?
           ORDER BY av.att_value_text, at.att_type_code LIMIT ?""",
        [like, limit],
    )
    # Attribute TYPES whose own name matches (e.g. searching "flavour").
    type_rows = await db.execute_fetchall(
        """SELECT at.att_type_code, at.att_type_text
           FROM attribute_types at
           WHERE at.att_type_text LIKE ?
           ORDER BY at.att_type_text LIMIT ?""",
        [like, limit],
    )

    async def bricks_for(att_type_code):
        rows = await db.execute_fetchall(
            """SELECT b.brick_code, b.description
               FROM brick_attribute_types bat
               JOIN bricks b ON bat.brick_code = b.brick_code
               WHERE bat.att_type_code = ?
               ORDER BY b.brick_code""",
            [att_type_code],
        )
        return [BrickItem(brick_code=r[0], description=r[1]) for r in rows]

    matches = []
    for r in value_rows:
        matches.append(AttributeMatch(
            kind="value", att_value_code=r[0], att_value_text=r[1],
            att_type_code=r[2], att_type_text=r[3],
            bricks=await bricks_for(r[2]),
        ))
    for r in type_rows:
        matches.append(AttributeMatch(
            kind="type", att_type_code=r[0], att_type_text=r[1],
            bricks=await bricks_for(r[0]),
        ))
    return matches, len(value_rows) + len(type_rows)


@router.get("/search/", response_model=SearchResponse, summary="Search across all GPC entities")
async def search_gpc(
    q: str = Query("", max_length=200, description="Search query"),
    category: Literal[
        "all", "segments", "families", "classes", "bricks", "attributes"
    ] = Query("all", description="Category filter"),
    limit: int = Query(
        DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT,
        description="Maximum results per entity type",
    ),
):
    """Search codes and descriptions across the GPC hierarchy.

    Searches segment/family/class/brick codes and descriptions, and — crucially —
    attribute types and values, which is where GPC keeps the specifics. A search
    for "olive" finds no brick by that name, but finds the "OLIVE OIL" attribute
    value and the "Oils Edible" brick it belongs to.

    Results are capped per entity type. Unbounded, a single-character query
    matches most of the taxonomy — `?q=e` returns over 900 rows — so every
    caller pays for a response nobody asked for. `counts` reports the real
    number of matches so a truncated answer is visible rather than silent.
    """
    if not q:
        return SearchResponse()

    db = await get_db()
    result = SearchResponse()
    like = f"%{q}%"

    for name, table, code_column, model in _SEARCHABLE:
        if category not in ("all", name):
            continue
        items, total = await _search_entity(db, table, code_column, model, like, limit)
        setattr(result, name, items)
        result.counts[name] = total
        if total > len(items):
            result.truncated = True

    if category in ("all", "attributes"):
        attributes, total = await _search_attributes(db, like, limit)
        result.attributes = attributes
        result.counts["attributes"] = total
        if total > len(attributes):
            result.truncated = True

    return result


# ── Curated FDC-category -> GPC mapping viewer ──────────────────────────

@router.get("/mappings", response_model=MappingsResponse,
            summary="Curated FDC-category -> GPC mappings, with live coverage")
async def gpc_mappings():
    """Every hand-verified entry in gpc_match.py's two curated tables, each
    resolved to its full GPC hierarchy, plus how much of the real local FDC
    corpus the tables reach.

    This is the working surface for the ongoing FDC-curation effort (see
    ARCH.md, "GPC Category Matching") — a place to see what is mapped, at
    which level, and what is still uncovered, without reading the source.
    """
    db = await get_db()
    brick_hierarchies, class_hierarchies = await asyncio.gather(
        gpc_match.hierarchy_for_bricks(db, gpc_match.FDC_CATEGORY_TO_BRICK.values()),
        gpc_match.hierarchy_for_classes(db, gpc_match.FDC_CATEGORY_TO_CLASS.values()),
    )
    mappings = [
        CuratedMapping(category=category, level="brick", code=code,
                       hierarchy=brick_hierarchies.get(code, []))
        for category, code in sorted(gpc_match.FDC_CATEGORY_TO_BRICK.items())
    ] + [
        CuratedMapping(category=category, level="class", code=code,
                       hierarchy=class_hierarchies.get(code, []))
        for category, code in sorted(gpc_match.FDC_CATEGORY_TO_CLASS.items())
    ]
    coverage = gpc_match.coverage_report()
    return MappingsResponse(
        mappings=mappings,
        coverage=MappingCoverage(**coverage) if coverage else None,
    )
