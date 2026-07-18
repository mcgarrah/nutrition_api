"""
DataOrchestrator — merges data from USDA FDC, Open Food Facts, and GS1 GPC
into a single CanonicalProduct response.

Reconciliation logic (layered approach from design doc):
  Layer 1: Open Food Facts — name, brand, image, ingredients, allergens,
           labels, and provisional nutrition. Applied first, as the base —
           USDA overrides every one of these fields it also carries.
  Layer 2: USDA FDC — the authoritative source, ahead of OFF wherever both
           can supply a field: nutrition, product name, brand, and
           ingredients all override OFF's when USDA has them. OFF's image,
           allergens, and labels survive untouched because USDA FDC's
           branded-food data does not carry those fields at all — there is
           nothing for USDA to override them with.
  Layer 3: GS1 GPC — category taxonomy. FDC's own category is tried first,
           through a hand-verified table (gpc_match.py); OFF's informal tags,
           matched by best-effort text search, are the fallback.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
import math
import os
import time
from urllib.parse import urlsplit

from cachetools import TTLCache
from usda_fdc.exceptions import FdcRateLimitError

from .models import CanonicalProduct, NutrientValue, SourceProvenance
from . import fdc_local
from . import gpc_match
from . import off_local
from . import usda_fdc
from . import open_food_facts as off
from . import attribution
from . import nutrients as nutrient_spec
from . import ratelimit
from . import resilience
from .resilience import off_breaker, usda_breaker, CircuitOpenError
from ..database import get_db

logger = logging.getLogger(__name__)

# In-memory TTL cache for hot GTIN lookups (Phase 2). Food data is
# effectively static, so a short TTL is safe and cuts upstream round-trips
# for popular items. Only complete-enough results are cached (see lookup()).
CACHE_MAX_SIZE = int(os.environ.get("LOOKUP_CACHE_MAX_SIZE", "1024"))
CACHE_TTL_S = float(os.environ.get("LOOKUP_CACHE_TTL_S", "300"))
_lookup_cache: TTLCache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL_S)

# A USDA barcode lookup costs two round trips: the fuzzy search, and then the
# food it matched. The store collapses this to one on a repeat visit, but a
# cold lookup needs the budget for both.
USDA_LOOKUP_ROUND_TRIPS = 2

# Provenance tag for a source served from the live upstream API rather than a
# local copy.
_LIVE = {"origin": "live"}


async def _fetch_off(barcode: str, fresh: bool = False):
    """Fetch from Open Food Facts, returning (data, latency_ms, provenance).

    The local copy of the daily export answers first — in microseconds, without
    spending one of OFF's 15-per-minute-per-IP tokens. A miss falls through to
    the live API, which stays authoritative for products newer than the export
    or too sparse to have been imported.

    `fresh=True` skips the local copy (and the disk store beneath the wrapper)
    and goes straight to the live API — for a caller who has asked for the newest
    data the upstream has, not the copy we hold.
    """
    start = time.monotonic()

    if not fresh and off_local.is_available():
        try:
            local = await off_local.get_by_gtin(barcode)
        except Exception as e:
            # A broken local copy must never take the upstream down with it.
            logger.warning("Local OFF lookup failed for %s: %s", barcode, e)
            local = None
        if local is not None:
            return local, (time.monotonic() - start) * 1000, off_local.provenance()
        logger.debug("Local OFF has no %s; asking the API", barcode)

    try:
        data = await off_breaker.call(
            lambda: off.get_product(barcode, use_store=not fresh))
    except ratelimit.RateLimitedError as e:
        # The budget is spent, so the call was never made. Degrade to a partial
        # result rather than overrunning Open Food Facts' allowance — their
        # remedy for that is an IP ban. The breaker does not count this: our
        # own busy minute says nothing about their health.
        logger.warning("OFF budget exhausted for %s (%s)", barcode, e)
        data = None
    except CircuitOpenError:
        logger.info("OFF circuit open; skipping fetch for %s", barcode)
        data = None
    except asyncio.TimeoutError:
        logger.warning("OFF fetch timed out for %s", barcode)
        data = None
    except Exception as e:
        logger.warning("OFF fetch failed for %s: %s", barcode, e)
        data = None
    elapsed = (time.monotonic() - start) * 1000
    return data, elapsed, (_LIVE if data else None)


async def _fetch_usda(barcode: str, fresh: bool = False):
    """Fetch from USDA FDC by UPC, returning (data, latency_ms, provenance).

    The local copy of the bulk dataset answers first. It holds every branded
    product FDC knew about at the last release, which is the overwhelming
    majority of what gets scanned, and it answers in microseconds without an API
    key, a rate-limit token, or a network call that can time out.

    A miss is not a failure — it means the product is newer than the dataset, or
    was never in it — so the request falls through to the live API, which is
    still the authority for anything the local copy has not got.

    `fresh=True` skips the local copy and the disk store to query the live API.
    """
    start = time.monotonic()

    if not fresh and fdc_local.is_available():
        try:
            local = await fdc_local.get_by_gtin(barcode)
        except Exception as e:
            # A broken local copy must never take the upstream down with it.
            logger.warning("Local FDC lookup failed for %s: %s", barcode, e)
            local = None
        if local is not None:
            return local, (time.monotonic() - start) * 1000, fdc_local.provenance()
        logger.debug("Local FDC has no %s; asking the API", barcode)

    try:
        # search_by_upc is two round trips to FDC — a search, then the food it
        # matched — and each is separately bounded by the client's own socket
        # timeout. Giving the pair a single call's allowance timed the second
        # one out and silently dropped USDA from the response.
        data = await usda_breaker.call(
            lambda: usda_fdc.search_by_upc(barcode, use_store=not fresh),
            timeout=resilience.UPSTREAM_TIMEOUT_S * USDA_LOOKUP_ROUND_TRIPS,
        )
    except (ratelimit.RateLimitedError, FdcRateLimitError) as e:
        # Either we refused our own call to stay inside FDC's ceiling, or FDC
        # refused it for us. Both are budgeting facts rather than outages, so
        # they degrade the response without blaming the upstream's health.
        logger.warning("USDA rate limited for %s (%s)", barcode, e)
        data = None
    except CircuitOpenError:
        logger.info("USDA circuit open; skipping fetch for %s", barcode)
        data = None
    except asyncio.TimeoutError:
        logger.warning("USDA fetch timed out for %s", barcode)
        data = None
    except Exception as e:
        logger.warning("USDA fetch failed for %s: %s", barcode, e)
        data = None
    elapsed = (time.monotonic() - start) * 1000
    return data, elapsed, (_LIVE if data else None)


async def _fetch_gpc_categories(off_categories: list[str]) -> tuple[list[str], float]:
    """Try to map OFF category tags to GPC hierarchy via search.

    This is a best-effort mapping — OFF categories are informal tags,
    not GPC codes. We search the GPC bricks for matching terms.
    Returns (category_list, latency_ms).
    """
    start = time.monotonic()
    hierarchy = []
    if not off_categories:
        return hierarchy, (time.monotonic() - start) * 1000

    try:
        db = await get_db()
        # Use the first few OFF category tags as search terms
        for tag in off_categories[:3]:
            # OFF tags look like "en:beverages" — extract the label
            label = tag.split(":")[-1].replace("-", " ") if ":" in tag else tag
            rows = await db.execute_fetchall(
                """SELECT b.brick_code, b.description, b.class_code,
                          c.description AS cls_desc, c.family_code,
                          f.description AS fam_desc, f.segment_code,
                          s.description AS seg_desc
                   FROM bricks b
                   LEFT JOIN classes c ON b.class_code = c.class_code
                   LEFT JOIN families f ON c.family_code = f.family_code
                   LEFT JOIN segments s ON f.segment_code = s.segment_code
                   WHERE b.description LIKE ?
                   LIMIT 1""",
                [f"%{label}%"],
            )
            if rows:
                r = rows[0]
                parts = [p for p in [r[7], r[5], r[3], r[1]] if p]
                if parts:
                    hierarchy = parts
                    break
    except Exception as e:
        logger.warning("GPC category lookup failed: %s", e)

    elapsed = (time.monotonic() - start) * 1000
    return hierarchy, elapsed


def _num(value) -> float | None:
    """Coerce an upstream scalar to a finite float, or None if it isn't one.

    Two failure modes, both of which reach the client as a broken response:

    * Not a number at all. Open Food Facts nutriments are crowdsourced and
      arrive as whatever was typed off the label — ">100", "trace", "" all
      occur. float() raises on those, and an unhandled ValueError turns a
      partial result into a 500, which this service must never return.

    * NaN or Infinity. These *are* floats, so they pass float() silently, but
      JSON has no literal for them: they serialize as the bare tokens NaN and
      Infinity, which strict parsers reject — poisoning the whole response,
      not just the one field. Python's json module accepts them on input, so
      they can arrive from upstream, and the string "nan" converts happily.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("Discarding non-numeric value %r", value)
        return None
    if not math.isfinite(number):
        logger.warning("Discarding non-finite value %r", value)
        return None
    return number


def _nv(value, unit="g") -> NutrientValue | None:
    """Create a NutrientValue, or None if the value isn't a finite number."""
    number = _num(value)
    if number is None:
        return None
    return NutrientValue(value=number, unit=unit)


def _text(value) -> str | None:
    """Accept an upstream free-text field only if it really is text.

    Pydantic does not validate on assignment, so a dict or a number assigned
    here survives all the way into the serialized response — the model's type
    annotations are decorative for anything the orchestrator sets.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


def _http_url(value) -> str | None:
    """Accept only an http(s) URL.

    image_url comes from Open Food Facts, which is crowdsourced — the value is
    attacker-influenceable, and we hand it to every consumer that renders it.
    Unvalidated, "javascript:...", "data:text/html,<script>..." and
    "file:///etc/passwd" all passed straight through. SPECS.md has always
    documented this field as an HttpUrl; nothing enforced it.
    """
    text = _text(value)
    if not text:
        return None
    parts = urlsplit(text.strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        return text.strip()
    logger.warning("Discarding non-http image URL %r", value)
    return None


def _str_list(value) -> list[str]:
    """Coerce an upstream tag list.

    OFF records are sparse and user-contributed: a key can be present but
    null, or hold non-string members. These fields are documented as always
    being a list, so a null here would break every consumer that iterates
    them without a None check.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _usda_nutrient(nutrients: dict, name: str) -> float | None:
    """Extract a nutrient amount from USDA nutrients dict by name."""
    entry = nutrients.get(name)
    if isinstance(entry, dict) and entry.get("amount") is not None:
        return entry["amount"]
    return None


def _cache_key(gtin: str) -> str:
    """Key the cache on the normalized barcode.

    GTIN-8/12/13/14 are the same identifier at different zero-paddings, so
    "028400642255" and "28400642255" name one product. Keying on the raw
    string gives them separate entries and duplicate upstream fetches for
    data we already hold.
    """
    return usda_fdc.normalize_gtin(gtin) or gtin


def _apply_nutrients(product: CanonicalProduct, values: dict) -> None:
    """Write nutrient values onto the product in the units *we* publish.

    A source that lacks a nutrient must never erase one another source
    supplied, so only present values are written.
    """
    for spec in nutrient_spec.NUTRIENTS:
        if spec.field not in values:
            continue
        if spec.field == "calories_kcal":
            number = _num(values[spec.field])
            if number is not None:
                product.calories_kcal = number
            continue
        nutrient = _nv(values[spec.field], unit=spec.unit)
        if nutrient is not None:
            setattr(product, spec.field, nutrient)


async def lookup(gtin: str, fresh: bool = False) -> CanonicalProduct:
    """Look up a product by GTIN/UPC and merge data from all sources.

    Fires OFF and USDA queries in parallel, then layers the results.
    Results with data are cached in-memory for CACHE_TTL_S seconds;
    misses are not cached so transient upstream failures can recover.

    `fresh=True` bypasses every cache layer — the in-memory cache, the local
    bulk copies, and the disk store — and queries the live upstream APIs. The
    result still populates the caches, so a forced refresh also updates them.
    """
    key = _cache_key(gtin)
    if not fresh:
        hit = _lookup_cache.get(key)
        if hit is not None:
            # Deep copy so callers can't mutate the cached entry
            product = hit.model_copy(deep=True)
            # Echo the barcode as the caller wrote it, not as it was first cached
            product.gtin = gtin
            product.cached = True
            return product

    # Parallel fetch from OFF and USDA
    (off_data, off_ms, off_prov), (usda_data, usda_ms, usda_prov) = await asyncio.gather(
        _fetch_off(gtin, fresh),
        _fetch_usda(gtin, fresh),
    )

    product = CanonicalProduct(gtin=gtin)
    product.upstream_latency_ms["OpenFoodFacts"] = round(off_ms, 1)
    product.upstream_latency_ms["USDA_FDC"] = round(usda_ms, 1)

    # --- Layer 1: Open Food Facts (name, image, ingredients, provisional nutrition) ---
    if off_data:
        product.data_sources.append("OpenFoodFacts")
        if off_prov:
            product.provenance["OpenFoodFacts"] = SourceProvenance(**off_prov)
        product.product_name = _text(off_data.get("product_name")) or product.product_name
        product.brand = _text(off_data.get("brands")) or product.brand
        product.image_url = _http_url(off_data.get("image_url"))
        product.ingredients_text = _text(off_data.get("ingredients_text"))
        product.allergens = _str_list(off_data.get("allergens"))
        product.labels = _str_list(off_data.get("labels"))

        # Provisional nutrition from OFF (per 100g). Values arrive in grams
        # even for nutrients a label shows in mg — from_off converts them.
        _apply_nutrients(product, nutrient_spec.from_off(
            off_data.get("nutrients_per_100g") or {}
        ))

    # --- Layer 2: USDA FDC (authoritative nutrition overrides OFF) ---
    if usda_data:
        product.data_sources.append("USDA_FDC")
        if usda_prov:
            product.provenance["USDA_FDC"] = SourceProvenance(**usda_prov)
        # USDA name overrides OFF if available
        usda_desc = _text(usda_data.get("description"))
        if usda_desc:
            product.product_name = usda_desc
        # USDA brand overrides OFF
        usda_brand = _text(usda_data.get("brand_owner")) or _text(usda_data.get("brand_name"))
        if usda_brand:
            product.brand = usda_brand

        # USDA is authoritative for nutrition and overrides OFF. Selected by
        # nutrient *id*: FDC publishes energy twice under the same name
        # ("Energy" in kcal and in kJ), so matching by name served whichever
        # arrived last — cheddar at 1710 kcal instead of 409.
        usda_nutrients = usda_data.get("nutrients")
        if isinstance(usda_nutrients, list):
            _apply_nutrients(product, nutrient_spec.from_usda(usda_nutrients))

        # USDA's ingredients (official label text) override OFF's
        # crowd-sourced text whenever USDA has them -- USDA FDC is
        # authoritative ahead of OFF across every field both sources can
        # supply, not just nutrition. OFF's text survives only when USDA
        # has none.
        usda_ingredients = _text(usda_data.get("ingredients"))
        if usda_ingredients:
            product.ingredients_text = usda_ingredients

    # --- Layer 3: GS1 GPC (category taxonomy) ---
    # FDC's own category is tried FIRST, through a hand-verified table
    # (gpc_match.py) rather than text matching. When it hits, the result is
    # trustworthy in a way fuzzy matching against OFF's free-form tags never
    # is, so it wins outright and the fuzzy path below is skipped. Brick-level
    # matches (specific) are tried before class-level ones (coarser, used
    # when we're confident about the category but not a single brick within
    # it) — curated_hierarchy_for_fdc_category does both in one call.
    gpc_ms = 0.0
    fdc_category = usda_data.get("category") if usda_data else None
    if gpc_match.curated_brick_for_fdc_category(fdc_category) or \
            gpc_match.curated_class_for_fdc_category(fdc_category):
        start = time.monotonic()
        db = await get_db()
        curated_hierarchy = await gpc_match.curated_hierarchy_for_fdc_category(db, fdc_category)
        gpc_ms += (time.monotonic() - start) * 1000
        if curated_hierarchy:
            product.category_hierarchy = curated_hierarchy
            product.category_hierarchy_source = "fdc_curated"
            product.data_sources.append("GS1_GPC")

    if not product.category_hierarchy:
        off_categories = _str_list(off_data.get("categories")) if off_data else []
        fuzzy_hierarchy, fuzzy_ms = await _fetch_gpc_categories(off_categories)
        gpc_ms += fuzzy_ms
        if fuzzy_hierarchy:
            product.category_hierarchy = _str_list(fuzzy_hierarchy)
            product.category_hierarchy_source = "off_fuzzy"
            product.data_sources.append("GS1_GPC")
        elif off_categories:
            # Last resort: raw OFF tags, not a GPC classification at all --
            # source stays "none" so a caller can't mistake upstream labels
            # for a verified match.
            product.category_hierarchy = [
                tag.split(":")[-1].replace("-", " ").title() if ":" in tag else tag
                for tag in off_categories[:5]
            ]

    product.upstream_latency_ms["GS1_GPC"] = round(gpc_ms, 1)

    # Attribution is a licence condition for Open Food Facts (ODbL), so it
    # accompanies the data it describes rather than living only in the docs.
    product.attribution = attribution.for_sources(product.data_sources)

    if product.data_sources:
        _lookup_cache[key] = product.model_copy(deep=True)

    return product
