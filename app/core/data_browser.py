"""
Read-only browser over the local data stores.

Backs a single "Data Browser" UI that switches between the SQLite databases the
service keeps on disk — Open Food Facts, USDA FDC, GS1 GPC — and the file-based
response store, presenting each with its schema, its rows, and per-column
coverage so their contents (and their duplication/sparsity) can be inspected.

Everything here is read-only and defensive. SQLite connections are opened in
read-only mode, and every table or column name that reaches SQL is validated
against the database's own introspection first — user input is only ever a bound
parameter, never an identifier — so the browser cannot be turned into an
arbitrary-query or write primitive.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import json
import sqlite3
from pathlib import Path

from . import store

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

MAX_ROWS = 200          # hard cap on a page of rows
MAX_OFFSET = 500_000    # deep paging past this is refused (OFFSET is O(n))
_CELL_LIMIT = 300       # long text values are truncated for the row grid


# ── Store registry ────────────────────────────────────────────────────

class SqliteStore:
    """A local SQLite database exposed as a set of browsable tables."""

    kind = "sqlite"

    def __init__(self, store_id, label, description, filename):
        self.id = store_id
        self.label = label
        self.description = description
        self.path = DATA_DIR / filename

    def available(self):
        return self.path.exists()

    def _conn(self):
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


class ResponseStore:
    """The file-based upstream response cache, presented as one 'table' per
    logical namespace: off/product, and usda (usda/upc merged with usda/food
    — see _usda_merged_records)."""

    kind = "filestore"

    def __init__(self, store_id, label, description):
        self.id = store_id
        self.label = label
        self.description = description

    def available(self):
        return store.STORE_ENABLED and store.STORE_DIR.exists()


_STORES = {
    "off": SqliteStore("off", "Open Food Facts",
                       "Local copy of the OFF daily export (products).",
                       "off.sqlite3"),
    "fdc": SqliteStore("fdc", "USDA FDC",
                       "Local copy of the FDC branded bulk dataset (foods).",
                       "fdc.sqlite3"),
    "gpc": SqliteStore("gpc", "GS1 GPC",
                       "The Global Product Classification taxonomy.",
                       "gpc.sqlite3"),
    "store": ResponseStore("store", "Response store",
                           "Cached upstream responses on disk, by namespace."),
}


def get_store(store_id):
    return _STORES.get(store_id)


# ── A tiny mtime-keyed cache for the expensive full scans ─────────────

_cache: dict = {}


def _cached(key, mtime, compute):
    hit = _cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    value = compute()
    _cache[key] = (mtime, value)
    return value


# ── SQLite introspection + browsing ──────────────────────────────────

def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _columns(conn, table):
    # `table` must already be validated against _tables(); PRAGMA won't take a
    # bound parameter, so the identifier is quoted after that check.
    return [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])}
            for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _sqlite_schema(s: SqliteStore):
    conn = s._conn()
    try:
        out = []
        for table in _tables(conn):
            cols = _columns(conn, table)
            fks = [{"column": r[3], "references_table": r[2], "references_column": r[4]}
                   for r in conn.execute(f'PRAGMA foreign_key_list("{table}")')]
            rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            out.append({"name": table, "columns": cols,
                        "foreign_keys": fks, "rows": rows})
        return out
    finally:
        conn.close()


def _truncate(value):
    if isinstance(value, str) and len(value) > _CELL_LIMIT:
        return value[:_CELL_LIMIT] + "…"
    return value


def _sqlite_rows(s, table, limit, offset, q, sort, direction):
    conn = s._conn()
    try:
        tables = _tables(conn)
        if table not in tables:
            return None
        cols = _columns(conn, table)
        names = [c["name"] for c in cols]

        limit = max(1, min(int(limit), MAX_ROWS))
        offset = max(0, min(int(offset), MAX_OFFSET))

        # ORDER BY — only by a real column, else the primary key / first column.
        if sort in names:
            column = sort
        else:
            column = next((c["name"] for c in cols if c["pk"]), names[0])
        order = f'ORDER BY "{column}" {"DESC" if direction == "desc" else "ASC"}'

        # Search — LIKE across the text columns, values bound.
        where, params = "", []
        if q:
            text_cols = [c["name"] for c in cols
                         if (c["type"] or "").upper() in ("TEXT", "")]
            if text_cols:
                where = "WHERE " + " OR ".join(f'"{c}" LIKE ?' for c in text_cols)
                params = [f"%{q}%"] * len(text_cols)

        total = _row_count(s, conn, table)
        matched = total
        if where:
            matched = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" {where}', params).fetchone()[0]

        sql = f'SELECT * FROM "{table}" {where} {order} LIMIT ? OFFSET ?'
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return {
            "columns": names,
            "rows": [[_truncate(r[c]) for c in names] for r in rows],
            "total": total,
            "matched": matched,
            "limit": limit,
            "offset": offset,
        }
    finally:
        conn.close()


def _row_count(s, conn, table):
    return _cached((s.id, table, "count"), s.path.stat().st_mtime,
                   lambda: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


_NUMERIC_TYPES = ("REAL", "INTEGER", "NUM", "FLOAT", "DOUBLE")


def _sqlite_numeric_columns(s, table):
    def compute():
        conn = s._conn()
        try:
            if table not in _tables(conn):
                return None
            return [c["name"] for c in _columns(conn, table)
                    if any(t in (c["type"] or "").upper() for t in _NUMERIC_TYPES)]
        finally:
            conn.close()
    if not s.path.exists():
        return None
    return _cached((s.id, table, "numeric_columns"), s.path.stat().st_mtime, compute)


def _percentile(conn, table, column, p):
    """The value at the p-th percentile (0-100) of a numeric column, via the
    standard offset-into-sorted-order trick -- SQLite has no PERCENTILE_CONT.
    One sort per call; only ever called twice per histogram (p1, p99), not
    once per bucket.
    """
    n = conn.execute(
        f'SELECT COUNT("{column}") FROM "{table}" WHERE "{column}" IS NOT NULL'
    ).fetchone()[0]
    if n == 0:
        return None
    offset = max(0, min(n - 1, round(n * p / 100)))
    row = conn.execute(
        f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
        f'ORDER BY "{column}" LIMIT 1 OFFSET ?',
        (offset,),
    ).fetchone()
    return row[0] if row else None


def _sqlite_histogram(s, table, column, bins):
    def compute():
        conn = s._conn()
        try:
            if table not in _tables(conn):
                return None
            names = [c["name"] for c in _columns(conn, table)]
            if column not in names:
                return None
            true_min, true_max, non_null = conn.execute(
                f'SELECT MIN("{column}"), MAX("{column}"), COUNT("{column}") FROM "{table}"'
            ).fetchone()
            total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if true_min is None or non_null == 0:
                return {"min": None, "max": None, "non_null": 0, "total": total,
                        "below_range": 0, "above_range": 0, "buckets": []}
            if true_min == true_max:
                # A single distinct value: one bucket, not a divide-by-zero.
                return {"min": true_min, "max": true_max, "non_null": non_null,
                        "total": total, "below_range": 0, "above_range": 0,
                        "buckets": [{"lo": true_min, "hi": true_max, "count": non_null}]}

            # Bin the 1st-99th percentile range, not the true min-max. A
            # single garbage value (a raw mirror can carry one -- e.g. a
            # calories_kcal in the 10^16 range that app/core/nutrients.py's
            # physical-max check would reject at lookup time, but which is
            # still sitting in the unfiltered local copy) would otherwise
            # stretch equal-width bins across a range 99.9% of real values
            # never come near, collapsing the actual distribution into one
            # bucket. below_range/above_range count what got clipped, so an
            # outlier is reported, not hidden.
            lo = _percentile(conn, table, column, 1)
            hi = _percentile(conn, table, column, 99)
            if lo == hi:
                lo, hi = true_min, true_max

            width = (hi - lo) / bins
            bounds = [
                (lo + i * width, hi if i == bins - 1 else lo + (i + 1) * width)
                for i in range(bins)
            ]
            # One pass, one SUM(CASE...) per bucket rather than `bins` separate
            # queries -- the same "single scan over N expressions" shape as
            # _sqlite_coverage's per-column COUNT. The last bucket's upper
            # bound is inclusive (<=) so the maximum in-range value is
            # counted; every other bucket is a half-open [lo, hi) so no value
            # is double-counted at a boundary. Boundaries are bound
            # parameters, not interpolated into the SQL text, the same
            # discipline this module applies everywhere else -- computed
            # internally here, not user input, but no reason for an exception.
            exprs = [
                'SUM(CASE WHEN "{c}" < ? THEN 1 ELSE 0 END)'.format(c=column),
                'SUM(CASE WHEN "{c}" > ? THEN 1 ELSE 0 END)'.format(c=column),
            ]
            params = [lo, hi]
            for i, (b_lo, b_hi) in enumerate(bounds):
                op = "<=" if i == bins - 1 else "<"
                exprs.append(
                    f'SUM(CASE WHEN "{column}" >= ? AND "{column}" {op} ? THEN 1 ELSE 0 END)')
                params.extend([b_lo, b_hi])
            row = conn.execute(
                f'SELECT {", ".join(exprs)} FROM "{table}" WHERE "{column}" IS NOT NULL',
                params,
            ).fetchone()
            below_range, above_range, *counts = row
            buckets = [{"lo": b_lo, "hi": b_hi, "count": counts[i] or 0}
                       for i, (b_lo, b_hi) in enumerate(bounds)]
            return {"min": true_min, "max": true_max, "non_null": non_null,
                    "total": total, "below_range": below_range or 0,
                    "above_range": above_range or 0, "buckets": buckets}
        finally:
            conn.close()
    if not s.path.exists():
        return None
    bins = max(1, min(int(bins), 50))
    return _cached((s.id, table, column, bins, "histogram"), s.path.stat().st_mtime, compute)


def _sqlite_coverage(s, table):
    def compute():
        conn = s._conn()
        try:
            if table not in _tables(conn):
                return None
            cols = _columns(conn, table)
            total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            # One scan counts the non-nulls of every column at once.
            exprs = ", ".join(f'COUNT("{c["name"]}")' for c in cols)
            counts = conn.execute(f'SELECT {exprs} FROM "{table}"').fetchone()
            return {
                "total": total,
                "columns": [
                    {"name": c["name"], "non_null": counts[i],
                     "pct": round(counts[i] / total * 100, 1) if total else 0.0}
                    for i, c in enumerate(cols)
                ],
            }
        finally:
            conn.close()
    if not s.path.exists():
        return None
    return _cached((s.id, table, "coverage"), s.path.stat().st_mtime, compute)


# ── Response-store (file) adapter ────────────────────────────────────
#
# usda/upc and usda/food are two files on disk for one relationship: a barcode
# resolving to a food record. usda/upc is the entry point (key = GTIN, payload
# = the fdc_id it resolved to); usda/food is the linked record (key = fdc_id,
# payload = the food itself). Showing them as two disconnected tables split a
# single fact across two rows a reader had to cross-reference by hand, so they
# are joined here into one logical "usda" table: one row per GTIN with its food
# payload attached. off/product needs no such treatment — one OFF record is
# already everything OFF said about that barcode.

def _store_namespaces():
    """The logical tables the response store presents."""
    return [store.OFF_PRODUCT, "usda"]


def _raw_store_records(namespace):
    """The records actually on disk under one namespace directory."""
    root = store.STORE_DIR / namespace
    if not root.exists():
        return []
    records = []
    for path in sorted(root.rglob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


def _usda_merged_records():
    """usda/upc joined to usda/food by fdc_id, one row per product.

    A upc mapping with no food payload attached would be the least useful half
    of the pair to show alone. Food records fetched without ever being looked
    up by barcode are real too (GET /api/v1/usda/food/{id} populates usda/food
    on its own) and are still surfaced, keyed by fdc_id since there is no GTIN
    for them.
    """
    upc_records = _raw_store_records(store.USDA_UPC)
    food_by_id = {str(r.get("key")): r for r in _raw_store_records(store.USDA_FOOD)}

    merged = []
    linked_ids = set()
    for upc in upc_records:
        fdc_id = upc.get("payload")
        food = food_by_id.get(str(fdc_id))
        if food is not None:
            linked_ids.add(str(fdc_id))
        merged.append({
            "key": upc.get("key"),
            "fetched_at": upc.get("fetched_at"),
            "schema_version": upc.get("schema_version"),
            "payload": {
                "fdc_id": fdc_id,
                "food": food.get("payload") if food else None,
                "food_fetched_at": food.get("fetched_at") if food else None,
            },
        })

    for fdc_id, food in food_by_id.items():
        if fdc_id in linked_ids:
            continue
        merged.append({
            "key": f"fdc:{fdc_id}",
            "fetched_at": food.get("fetched_at"),
            "schema_version": food.get("schema_version"),
            "payload": {"fdc_id": food.get("key"), "food": food.get("payload"),
                        "food_fetched_at": food.get("fetched_at")},
        })
    return merged


def _store_records(namespace):
    if namespace == "usda":
        return _usda_merged_records()
    return _raw_store_records(namespace)


def _filestore_schema(s: ResponseStore):
    out = []
    for ns in _store_namespaces():
        out.append({
            "name": ns,
            "columns": [
                {"name": "key", "type": "TEXT", "notnull": True, "pk": True},
                {"name": "fetched_at", "type": "TEXT", "notnull": True, "pk": False},
                {"name": "schema_version", "type": "TEXT", "notnull": False, "pk": False},
                {"name": "payload", "type": "JSON", "notnull": False, "pk": False},
            ],
            "foreign_keys": [],
            "rows": len(_store_records(ns)),
        })
    return out


def _filestore_rows(s, table, limit, offset, q, sort, direction):
    if table not in _store_namespaces():
        return None
    records = _store_records(table)
    if q:
        needle = q.lower()
        records = [r for r in records if needle in json.dumps(r).lower()]
    reverse = direction == "desc"
    if sort in ("key", "fetched_at", "schema_version"):
        records.sort(key=lambda r: str(r.get(sort, "")), reverse=reverse)
    else:
        records.sort(key=lambda r: str(r.get("fetched_at", "")), reverse=reverse)
    total = len(_store_records(table))
    limit = max(1, min(int(limit), MAX_ROWS))
    offset = max(0, int(offset))
    page = records[offset:offset + limit]
    names = ["key", "fetched_at", "schema_version", "payload"]
    return {
        "columns": names,
        "rows": [[r.get("key"), r.get("fetched_at"), r.get("schema_version"),
                  _truncate(json.dumps(r.get("payload"), ensure_ascii=False))]
                 for r in page],
        "total": total,
        "matched": len(records),
        "limit": limit,
        "offset": offset,
    }


def _filestore_coverage(s, table):
    if table not in _store_namespaces():
        return None
    records = _store_records(table)
    total = len(records)
    fields = ["key", "fetched_at", "schema_version", "payload"]
    return {
        "total": total,
        "columns": [
            {"name": f, "non_null": sum(1 for r in records if r.get(f) is not None),
             "pct": round(sum(1 for r in records if r.get(f) is not None) / total * 100, 1)
             if total else 0.0}
            for f in fields
        ],
    }


# ── Public API used by the routes ─────────────────────────────────────

def list_stores():
    out = []
    for s in _STORES.values():
        entry = {"id": s.id, "label": s.label, "description": s.description,
                 "kind": s.kind, "available": s.available()}
        if s.kind == "sqlite" and s.available():
            entry["size_mb"] = round(s.path.stat().st_size / 1e6, 1)
            entry["tables"] = [{"name": t["name"], "rows": t["rows"]}
                               for t in _sqlite_schema(s)]
        elif s.kind == "filestore" and s.available():
            entry["tables"] = [{"name": t["name"], "rows": t["rows"]}
                               for t in _filestore_schema(s)]
        out.append(entry)
    return out


def schema(store_id):
    s = get_store(store_id)
    if not s or not s.available():
        return None
    return _sqlite_schema(s) if s.kind == "sqlite" else _filestore_schema(s)


def rows(store_id, table, limit=50, offset=0, q="", sort="", direction="asc"):
    s = get_store(store_id)
    if not s or not s.available():
        return None
    fn = _sqlite_rows if s.kind == "sqlite" else _filestore_rows
    return fn(s, table, limit, offset, q, sort, direction)


def coverage(store_id, table):
    s = get_store(store_id)
    if not s or not s.available():
        return None
    fn = _sqlite_coverage if s.kind == "sqlite" else _filestore_coverage
    return fn(s, table)


def numeric_columns(store_id, table):
    """Column names in `table` worth histogramming (REAL/INTEGER-typed).

    SQLite-only -- the response store's file-backed "tables" are already a
    fixed four columns (key/fetched_at/schema_version/payload), none numeric.
    """
    s = get_store(store_id)
    if not s or s.kind != "sqlite" or not s.available():
        return None
    return _sqlite_numeric_columns(s, table)


def histogram(store_id, table, column, bins=20):
    """Bucketed value distribution for one numeric column.

    Not just non-null coverage (see `coverage`) -- a column that's 95%
    populated but every value clustered at one boundary (e.g. a nutrient
    capped at the physical-max sanity bound in app/core/nutrients.py) reads
    identically to a healthy column under a pure null-rate check. SQLite-only,
    same reasoning as `numeric_columns`.
    """
    s = get_store(store_id)
    if not s or s.kind != "sqlite" or not s.available():
        return None
    return _sqlite_histogram(s, table, column, bins)


def record(store_id, table, key):
    """A single response-store record's full JSON payload (not truncated)."""
    s = get_store(store_id)
    if not s or s.kind != "filestore" or table not in _store_namespaces():
        return None
    for r in _store_records(table):
        if r.get("key") == key:
            return r
    return None
