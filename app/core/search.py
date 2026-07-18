"""
Local-DB-backed product name search.

Searches the local FDC and OFF bulk copies by product name — free, fast, and
does not spend Open Food Facts' tight 15-request/minute budget on searches
instead of the GTIN lookups it is meant for. Consistent with the rest of the
service's local-first design (see fdc_local.py, off_local.py): the same
argument for a local barcode lookup — no API key, no rate limit, microseconds
instead of a round trip — applies just as well to a name search.

This is deliberately a lighter-weight result than CanonicalProduct: identity
fields only (barcode, name, brand, image), enough to render a result list and
let the caller pick one. The full merged nutrition data for a chosen result
comes from the existing GET /api/v1/lookup/{gtin} — one merge implementation,
not two.

A search that finds nothing here is not "no such product": it means neither
local copy's name text matched, not that no upstream has it. The existing
GTIN lookup still falls through to the live APIs for a barcode a caller
already has.

Querying prefers each mirror's FTS5 index (foods_fts / products_fts,
scripts/build_fdc_db.py / build_off_db.py) over a `LIKE '%q%'` scan: a
leading wildcard can never use an index, so LIKE is a full-table scan by
construction — measured at ~17s cold over the 1.2 GB OFF mirror (PLAN.md
item 2). FTS5 falls back to LIKE automatically for a mirror built before the
FTS table existed, so an unrefreshed database still works, just slower.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import re
import sqlite3

from . import fdc_local
from . import off_local

DEFAULT_RESULTS = 20
MAX_RESULTS = 50

_WORD = re.compile(r"\w+", re.UNICODE)


def _fts_match_expr(query: str) -> str | None:
    """Turn free text into an FTS5 MATCH expression: a prefix match per word.

    Each word becomes a quoted phrase token immediately followed by `*`
    (`"peanut"*`), which FTS5 treats as a literal prefix search — quoting
    neutralizes any FTS5 query-syntax characters the word might otherwise be
    read as. Words are ANDed together (FTS5's implicit default), so "peanut
    butter" requires both, in either order, anywhere in the field.

    Returns None if the query has no word characters at all (e.g. "!!!") —
    not a blank string, which search_products() already rejects earlier.
    """
    words = _WORD.findall(query.lower())
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _search_fdc(query: str, limit: int) -> list[dict]:
    if not fdc_local.is_available():
        return []
    conn = sqlite3.connect(f"file:{fdc_local.DB_PATH}?mode=ro", uri=True)
    try:
        match = _fts_match_expr(query)
        if match is not None and _table_exists(conn, "foods_fts"):
            rows = conn.execute(
                "SELECT f.gtin14, f.description, f.brand_owner, f.brand_name "
                "FROM foods_fts JOIN foods AS f USING (gtin14) "
                "WHERE foods_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT gtin14, description, brand_owner, brand_name "
                "FROM foods WHERE description LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "gtin": r[0], "product_name": r[1], "brand": r[2] or r[3],
            "image_url": None, "source": "USDA_FDC",
        }
        for r in rows
    ]


def _search_off(query: str, limit: int) -> list[dict]:
    if not off_local.is_available():
        return []
    conn = sqlite3.connect(f"file:{off_local.DB_PATH}?mode=ro", uri=True)
    try:
        match = _fts_match_expr(query)
        if match is not None and _table_exists(conn, "products_fts"):
            rows = conn.execute(
                "SELECT p.gtin14, p.product_name, p.brands, p.image_url "
                "FROM products_fts JOIN products AS p USING (gtin14) "
                "WHERE products_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT gtin14, product_name, brands, image_url "
                "FROM products WHERE product_name LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "gtin": r[0], "product_name": r[1], "brand": r[2],
            "image_url": r[3], "source": "OpenFoodFacts",
        }
        for r in rows
    ]


def search_products(query: str, limit: int = DEFAULT_RESULTS) -> list[dict]:
    """Search both local copies by product name, merged and deduplicated by GTIN.

    OFF is queried first and wins a collision: it is the source with real
    product photos and is the more consistent brand field for consumer-facing
    text. A GTIN found only in FDC is still returned rather than dropped —
    half a result is better than a missing one, and the popup that opens on
    a click resolves the rest through the normal merged lookup anyway.
    """
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(limit, MAX_RESULTS))

    merged: dict[str, dict] = {r["gtin"]: r for r in _search_off(query, limit)}
    for r in _search_fdc(query, limit):
        merged.setdefault(r["gtin"], r)

    return list(merged.values())[:limit]
