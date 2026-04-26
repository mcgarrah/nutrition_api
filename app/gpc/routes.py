"""
GS1 GPC API routes for FastAPI.

Provides the same endpoint contract as the shiny-shop Django REST Framework API,
but queries a corrected schema with junction tables for many-to-many relationships.

Endpoints:
  /api/gpc/segments/              - list (paginated, searchable)
  /api/gpc/segments/{code}        - detail with families
  /api/gpc/families/              - list (paginated, searchable, filterable by segment)
  /api/gpc/families/{code}        - detail with classes + parent breadcrumb
  /api/gpc/classes/               - list (paginated, searchable, filterable by family)
  /api/gpc/classes/{code}         - detail with bricks + parent breadcrumb
  /api/gpc/bricks/                - list (paginated, searchable, filterable by class)
  /api/gpc/bricks/{code}          - detail with attributes + parent breadcrumb
  /api/gpc/search/?q=...          - cross-entity search

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from fastapi import APIRouter, HTTPException, Query, Request
from ..database import get_db
from .models import (
    SegmentItem, SegmentDetail,
    FamilyItem, FamilyDetail, ParentSegmentRef,
    ClassItem, ClassDetail, ParentFamilyRef,
    BrickItem, BrickDetail, ParentClassRef,
    AttributeTypeItem, AttributeValueItem,
    PaginatedResponse, SearchResponse,
)

router = APIRouter(prefix="/api/gpc", tags=["GPC"])

DEFAULT_PAGE_SIZE = 20


def _paginate_url(request: Request, page: int | None, page_size: int) -> str | None:
    if page is None:
        return None
    url = str(request.url).split("?")[0]
    return f"{url}?page={page}&page_size={page_size}"


def _page_params(page: int, page_size: int, total: int):
    offset = (page - 1) * page_size
    next_page = page + 1 if offset + page_size < total else None
    prev_page = page - 1 if page > 1 else None
    return offset, next_page, prev_page


async def _count_and_fetch(table, columns, where="", params=None, order_by="", page=1, page_size=DEFAULT_PAGE_SIZE):
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


@router.get("/segments/{segment_code}", response_model=SegmentDetail, summary="Retrieve a GPC Segment")
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

@router.get("/search/", response_model=SearchResponse, summary="Search across all GPC entities")
async def search_gpc(
    q: str = Query("", description="Search query"),
    category: str = Query("all", description="Category filter", enum=["all", "segments", "families", "classes", "bricks"]),
):
    if not q:
        return SearchResponse()

    db = await get_db()
    result = SearchResponse()
    like = f"%{q}%"

    if category in ("all", "segments"):
        rows = await db.execute_fetchall(
            "SELECT segment_code, description FROM segments WHERE segment_code LIKE ? OR description LIKE ? ORDER BY segment_code",
            [like, like],
        )
        result.segments = [SegmentItem(segment_code=r[0], description=r[1]) for r in rows]

    if category in ("all", "families"):
        rows = await db.execute_fetchall(
            "SELECT family_code, description FROM families WHERE family_code LIKE ? OR description LIKE ? ORDER BY family_code",
            [like, like],
        )
        result.families = [FamilyItem(family_code=r[0], description=r[1]) for r in rows]

    if category in ("all", "classes"):
        rows = await db.execute_fetchall(
            "SELECT class_code, description FROM classes WHERE class_code LIKE ? OR description LIKE ? ORDER BY class_code",
            [like, like],
        )
        result.classes = [ClassItem(class_code=r[0], description=r[1]) for r in rows]

    if category in ("all", "bricks"):
        rows = await db.execute_fetchall(
            "SELECT brick_code, description FROM bricks WHERE brick_code LIKE ? OR description LIKE ? ORDER BY brick_code",
            [like, like],
        )
        result.bricks = [BrickItem(brick_code=r[0], description=r[1]) for r in rows]

    return result
