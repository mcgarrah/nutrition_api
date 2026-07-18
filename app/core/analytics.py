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


async def summary() -> dict:
    """The single aggregated payload GET /api/v1/data/analytics returns."""
    sources = await source_summary()
    return {
        "sources": sources,
        "nutrient_coverage": nutrient_field_coverage(),
        "gpc_matching": gpc_matching_summary(),
    }
