"""
DataOrchestrator — merges data from USDA FDC, Open Food Facts, and GS1 GPC
into a single CanonicalProduct response.

Reconciliation logic (layered approach from design doc):
  Layer 1: Open Food Facts — name, brand, image, ingredients, allergens, labels,
           and provisional nutrition (used only if USDA is missing).
  Layer 2: USDA FDC — authoritative nutrition data overrides OFF values.
           Product name from USDA overrides OFF if available.
  Layer 3: GS1 GPC — category taxonomy. OFF categories used as fallback.

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

from .models import CanonicalProduct, NutrientValue
from . import usda_fdc
from . import open_food_facts as off
from . import ratelimit
from .resilience import off_breaker, usda_breaker, CircuitOpenError
from ..database import get_db

logger = logging.getLogger(__name__)

# In-memory TTL cache for hot GTIN lookups (Phase 2). Food data is
# effectively static, so a short TTL is safe and cuts upstream round-trips
# for popular items. Only complete-enough results are cached (see lookup()).
CACHE_MAX_SIZE = int(os.environ.get("LOOKUP_CACHE_MAX_SIZE", "1024"))
CACHE_TTL_S = float(os.environ.get("LOOKUP_CACHE_TTL_S", "300"))
_lookup_cache: TTLCache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL_S)


async def _fetch_off(barcode: str) -> tuple[dict | None, float]:
    """Fetch from Open Food Facts, returning (data, latency_ms)."""
    start = time.monotonic()

    # Open Food Facts allows 15 reads/minute per IP and enforces it with an IP
    # ban. Spending a token we do not have is not a request that fails — it is
    # a request that gets the whole deployment blocked. Degrade instead.
    #
    # Note this is deliberately NOT recorded as a breaker failure: the call was
    # never made, and our own budget running dry says nothing about OFF's health.
    if not ratelimit.off_limiter.try_acquire():
        logger.warning(
            "OFF rate budget exhausted; skipping fetch for %s (retry in %.1fs)",
            barcode, ratelimit.off_limiter.retry_after(),
        )
        return None, (time.monotonic() - start) * 1000

    try:
        data = await off_breaker.call(lambda: off.get_product(barcode))
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
    return data, elapsed


async def _fetch_usda(barcode: str) -> tuple[dict | None, float]:
    """Fetch from USDA FDC by UPC, returning (data, latency_ms)."""
    start = time.monotonic()

    # USDA reports its ceiling in x-ratelimit-limit: 3600/hour. Overrunning it
    # gets the key throttled, which degrades every user rather than this one.
    if not ratelimit.usda_limiter.try_acquire():
        logger.warning(
            "USDA rate budget exhausted; skipping fetch for %s (retry in %.1fs)",
            barcode, ratelimit.usda_limiter.retry_after(),
        )
        return None, (time.monotonic() - start) * 1000

    try:
        data = await usda_breaker.call(lambda: usda_fdc.search_by_upc(barcode))
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
    return data, elapsed


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


async def lookup(gtin: str) -> CanonicalProduct:
    """Look up a product by GTIN/UPC and merge data from all sources.

    Fires OFF and USDA queries in parallel, then layers the results.
    Results with data are cached in-memory for CACHE_TTL_S seconds;
    misses are not cached so transient upstream failures can recover.
    """
    key = _cache_key(gtin)
    hit = _lookup_cache.get(key)
    if hit is not None:
        # Deep copy so callers can't mutate the cached entry
        product = hit.model_copy(deep=True)
        # Echo the barcode as the caller wrote it, not as it was first cached
        product.gtin = gtin
        product.cached = True
        return product

    # Parallel fetch from OFF and USDA
    (off_data, off_ms), (usda_data, usda_ms) = await asyncio.gather(
        _fetch_off(gtin),
        _fetch_usda(gtin),
    )

    product = CanonicalProduct(gtin=gtin)
    product.upstream_latency_ms["OpenFoodFacts"] = round(off_ms, 1)
    product.upstream_latency_ms["USDA_FDC"] = round(usda_ms, 1)

    # --- Layer 1: Open Food Facts (name, image, ingredients, provisional nutrition) ---
    if off_data:
        product.data_sources.append("OpenFoodFacts")
        product.product_name = _text(off_data.get("product_name")) or product.product_name
        product.brand = _text(off_data.get("brands")) or product.brand
        product.image_url = _http_url(off_data.get("image_url"))
        product.ingredients_text = _text(off_data.get("ingredients_text"))
        product.allergens = _str_list(off_data.get("allergens"))
        product.labels = _str_list(off_data.get("labels"))

        # Provisional nutrition from OFF (per 100g)
        nutr = off_data.get("nutrients_per_100g", {})
        product.calories_kcal = _num(nutr.get("calories_kcal"))
        product.protein = _nv(nutr.get("protein_g"))
        product.fat = _nv(nutr.get("fat_g"))
        product.carbohydrates = _nv(nutr.get("carbohydrates_g"))
        product.fiber = _nv(nutr.get("fiber_g"))
        product.sugars = _nv(nutr.get("sugars_g"))
        product.sodium = _nv(nutr.get("sodium_g"))

    # --- Layer 2: USDA FDC (authoritative nutrition overrides OFF) ---
    if usda_data:
        product.data_sources.append("USDA_FDC")
        # USDA name overrides OFF if available
        usda_desc = _text(usda_data.get("description"))
        if usda_desc:
            product.product_name = usda_desc
        # USDA brand overrides OFF
        usda_brand = _text(usda_data.get("brand_owner")) or _text(usda_data.get("brand_name"))
        if usda_brand:
            product.brand = usda_brand

        nutrients = usda_data.get("nutrients")
        if isinstance(nutrients, dict) and nutrients:
            # Override nutrition with USDA values (authoritative)
            energy = _num(_usda_nutrient(nutrients, "Energy"))
            if energy is not None:
                product.calories_kcal = energy
            protein = _usda_nutrient(nutrients, "Protein")
            if protein is not None:
                product.protein = _nv(protein)
            fat = _usda_nutrient(nutrients, "Total lipid (fat)")
            if fat is not None:
                product.fat = _nv(fat)
            carbs = _usda_nutrient(nutrients, "Carbohydrate, by difference")
            if carbs is not None:
                product.carbohydrates = _nv(carbs)
            fiber = _usda_nutrient(nutrients, "Fiber, total dietary")
            if fiber is not None:
                product.fiber = _nv(fiber)
            sugars = _usda_nutrient(nutrients, "Sugars, total including NLEA")
            if sugars is not None:
                product.sugars = _nv(sugars)
            sodium = _usda_nutrient(nutrients, "Sodium, Na")
            if sodium is not None:
                product.sodium = _nv(sodium, unit="mg")

        # Use USDA ingredients if OFF didn't have them
        if not product.ingredients_text:
            product.ingredients_text = _text(usda_data.get("ingredients"))

    # --- Layer 3: GS1 GPC (category taxonomy) ---
    off_categories = _str_list(off_data.get("categories")) if off_data else []
    gpc_hierarchy, gpc_ms = await _fetch_gpc_categories(off_categories)
    product.upstream_latency_ms["GS1_GPC"] = round(gpc_ms, 1)
    if gpc_hierarchy:
        product.category_hierarchy = _str_list(gpc_hierarchy)
        product.data_sources.append("GS1_GPC")
    elif off_categories:
        # Fallback: use OFF category tags as-is
        product.category_hierarchy = [
            tag.split(":")[-1].replace("-", " ").title() if ":" in tag else tag
            for tag in off_categories[:5]
        ]

    if product.data_sources:
        _lookup_cache[key] = product.model_copy(deep=True)

    return product
