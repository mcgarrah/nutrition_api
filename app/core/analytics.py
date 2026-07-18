"""
Data quality & coverage dashboard: a single aggregated view over analytics
that otherwise exist scattered across three places (GPC matching coverage
via gpc_match.py, per-column null-rate via data_browser.py, dataset
provenance via fdc_local.py/off_local.py) — see PLAN.md item 6.

The intended audience is a data engineer or data analyst, not an operator.
/api/v1/health already answers "is the service up"; this answers "how good
is the data we're actually producing, and where are the gaps" — the
question to ask before building an ML feature set on this API, or trusting
a bulk export of it for analysis.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3

from . import data_browser
from . import fdc_local
from . import gpc_match
from . import off_local
from . import nutrients as nutrient_spec
from ..database import get_db

# The two local mirrors' nutrient columns are named identically to
# NutrientSpec.field (see fdc_local.py/off_local.py, both built from the
# same NUTRIENTS tuple) -- comparing FDC vs. OFF coverage for the "same"
# field is therefore a column-name match, not a mapping table to maintain.
_NUTRIENT_FIELDS = [spec.field for spec in nutrient_spec.NUTRIENTS]

# "Agree" means within this fraction of whichever value is larger (floored
# at 1, so two negligible trace amounts -- 0.01g vs 0.02g fiber -- don't
# register as "100% different" by relative error alone). A documented,
# adjustable choice, not a regulatory figure: FDA label-rounding tolerance
# rules are a materially different question (permitted variance in a single
# lab-tested value against its label), not "do two independent sources
# describing the same product roughly agree."
_AGREEMENT_TOLERANCE = 0.15

_cross_source_cache: dict = {}


def nutrient_field_coverage() -> list[dict]:
    """Per-nutrient non-null % in the FDC and OFF local mirrors, side by
    side. The single most useful number here for someone building an ML
    feature set: which nutrients have enough real coverage to be usable,
    and whether FDC or OFF is the better source for a given one.

    A source with no local mirror contributes null percentages for every
    field, not an exception -- this always returns a full field list.
    """
    fdc_cov = data_browser.coverage("fdc", "foods")
    off_cov = data_browser.coverage("off", "products")
    fdc_by_col = {c["name"]: c for c in fdc_cov["columns"]} if fdc_cov else {}
    off_by_col = {c["name"]: c for c in off_cov["columns"]} if off_cov else {}

    return [
        {
            "field": field,
            "fdc_pct": fdc_by_col.get(field, {}).get("pct"),
            "fdc_non_null": fdc_by_col.get(field, {}).get("non_null"),
            "off_pct": off_by_col.get(field, {}).get("pct"),
            "off_non_null": off_by_col.get(field, {}).get("non_null"),
        }
        for field in _NUTRIENT_FIELDS
    ]


async def source_summary() -> dict:
    """Dataset identity and freshness for each local source: what version,
    how many rows, how big, how recently refreshed. The provenance half of
    "can I trust this," alongside the coverage/distribution half.
    """
    fdc = fdc_local.stats()
    off = off_local.stats()

    gpc: dict = {"status": "absent"}
    try:
        db = await get_db()
        counts = {}
        for table in ("segments", "families", "classes", "bricks"):
            row = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
            counts[table] = row[0][0]
        meta_rows = await db.execute_fetchall("SELECT key, value FROM gpc_metadata")
        metadata = {r[0]: r[1] for r in meta_rows}
        gpc = {
            "status": "ok",
            "version": metadata.get("gpc_version"),
            "xml_date": metadata.get("xml_date"),
            "imported_at": metadata.get("import_timestamp"),
            **counts,
        }
    except Exception as e:
        gpc = {"status": "error", "detail": str(e)}

    return {"fdc": fdc, "off": off, "gpc": gpc}


def gpc_matching_summary() -> dict:
    """The two curated-table coverage reports side by side.

    These measure different denominators -- fdc_coverage is a % of
    *categorized foods* in the FDC mirror; off_tag_coverage is a % of *tag
    occurrences* in the OFF mirror (one product usually carries several
    tags) -- so they are reported separately, not combined into one
    "% of everything classified" figure. Computing that true combined
    number would mean re-running the orchestrator's full three-tier
    precedence across the whole corpus, which this first draft does not do
    -- see PLAN.md item 6's "left for later" note.
    """
    return {
        "fdc_curated": gpc_match.coverage_report(),
        "reviewed": gpc_match.off_tag_coverage_report(),
    }


def _cross_source_compute() -> list[dict]:
    conn = sqlite3.connect(":memory:")
    try:
        # Read-only ATTACH, same as every other local-mirror query in this
        # codebase -- nothing here ever writes to either file.
        conn.execute(f"ATTACH DATABASE 'file:{fdc_local.DB_PATH}?mode=ro' AS fdcdb")
        conn.execute(f"ATTACH DATABASE 'file:{off_local.DB_PATH}?mode=ro' AS offdb")

        # Both build scripts always create a column for every field in
        # NUTRIENT_FIELDS, so in production this intersection is just
        # _NUTRIENT_FIELDS itself -- but assuming that without checking
        # would be one un-degraded assumption in a module that otherwise
        # degrades every missing piece gracefully (nutrient_field_coverage()
        # already tolerates a column absent from either mirror). A comparable
        # field a schema doesn't have yet gets reported as zero matched
        # pairs below, not a query error.
        fdc_cols = {r[1] for r in conn.execute("PRAGMA fdcdb.table_info(foods)")}
        off_cols = {r[1] for r in conn.execute("PRAGMA offdb.table_info(products)")}
        comparable = [f for f in _NUTRIENT_FIELDS if f in fdc_cols and f in off_cols]

        results = {
            field: {"field": field, "matched_gtins": 0, "agree": 0, "agree_pct": None}
            for field in _NUTRIENT_FIELDS
        }
        if not comparable:
            return list(results.values())

        # One pass over the whole join computes every comparable field's
        # stats at once -- the same "single scan, many expressions" shape as
        # data_browser._sqlite_coverage and _sqlite_histogram, measured at
        # ~6.6s for all 36 fields against the real corpus vs. ~36s+ done as
        # 36 separate queries. Field names come from the introspected column
        # list above (not user input) -- interpolated as identifiers is the
        # same trust model this codebase already applies to validated
        # column names elsewhere; every *value* here is a literal 1/0.15
        # constant, never external input.
        # off_local.py stores every nutrient in OFF's native unit, raw grams
        # per 100g -- including the ones we publish in mg/ug (from_off()'s
        # gram->mg/ug conversion runs at *lookup* time, not build time; see
        # nutrients.off_raw_to_published_scale). fdcdb.foods, by contrast,
        # already holds published-unit values (build_fdc_db.py calls
        # from_usda() at build time). Comparing the two raw would silently
        # compare different units for 26 of 36 fields -- caught live against
        # the real mirrors as an exact 1000x/1e6x ratio on every mismatch
        # before this scale factor was added.
        exprs = []
        for field in comparable:
            scale = nutrient_spec.off_raw_to_published_scale(field)
            off_expr = f'o."{field}"' if scale == 1.0 else f'(o."{field}" * {scale})'
            exprs.append(
                f'SUM(CASE WHEN f."{field}" IS NOT NULL AND o."{field}" IS NOT NULL '
                f'THEN 1 ELSE 0 END)')
            exprs.append(
                f'SUM(CASE WHEN f."{field}" IS NOT NULL AND o."{field}" IS NOT NULL '
                f'AND ABS(f."{field}" - {off_expr}) <= {_AGREEMENT_TOLERANCE} * '
                f'MAX(ABS(f."{field}"), ABS({off_expr}), 1) THEN 1 ELSE 0 END)')
        sql = (f'SELECT {", ".join(exprs)} '
               f'FROM fdcdb.foods f JOIN offdb.products o USING (gtin14)')
        row = conn.execute(sql).fetchone()

        for i, field in enumerate(comparable):
            matched, agree = row[i * 2] or 0, row[i * 2 + 1] or 0
            results[field] = {
                "field": field,
                "matched_gtins": matched,
                "agree": agree,
                "agree_pct": round(agree / matched * 100, 1) if matched else None,
            }
        return list(results.values())
    finally:
        conn.close()


def cross_source_agreement() -> dict | None:
    """For GTINs present in *both* local mirrors, how often do FDC and OFF
    agree on a nutrient value (within _AGREEMENT_TOLERANCE)?

    None if either mirror is unavailable -- there is nothing to join.
    Cached by both files' mtimes together: either one changing (a fresh
    build of either mirror) invalidates the cached result.
    """
    if not fdc_local.DB_PATH.exists() or not off_local.DB_PATH.exists():
        return None
    key = (fdc_local.DB_PATH.stat().st_mtime, off_local.DB_PATH.stat().st_mtime)
    hit = _cross_source_cache.get("fields")
    if hit is not None and hit[0] == key:
        return {"tolerance": _AGREEMENT_TOLERANCE, "fields": hit[1]}
    fields = _cross_source_compute()
    _cross_source_cache["fields"] = (key, fields)
    return {"tolerance": _AGREEMENT_TOLERANCE, "fields": fields}


async def summary() -> dict:
    """The single aggregated payload GET /api/v1/data/analytics returns."""
    sources = await source_summary()
    return {
        "sources": sources,
        "nutrient_coverage": nutrient_field_coverage(),
        "gpc_matching": gpc_matching_summary(),
        "cross_source_agreement": cross_source_agreement(),
    }
