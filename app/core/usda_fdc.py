"""
USDA FoodData Central service wrapper.

Wraps the synchronous usda_fdc.FdcClient for async use in FastAPI
by running blocking calls in a thread pool via asyncio.run_in_executor.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
from typing import Any

from dotenv import load_dotenv
from usda_fdc.exceptions import FdcResourceNotFoundError

from . import ratelimit
from . import resilience
from . import store

load_dotenv()

logger = logging.getLogger(__name__)

# Threads dedicated to USDA FDC, so a stall here cannot starve Open Food Facts
_executor = resilience.make_executor("usda")

# Lazy-initialized singleton client
_fdc_client = None
_fdc_available = None  # None = not checked, True/False = checked


def _get_fdc_client():
    """Return the FdcClient singleton, or None if no API key is configured."""
    global _fdc_client, _fdc_available
    if _fdc_available is False:
        return None
    if _fdc_client is not None:
        return _fdc_client
    try:
        from usda_fdc import FdcClient
        # Bound the socket itself, not just the await. asyncio.wait_for frees
        # the caller but cannot cancel the blocking SDK call underneath, so
        # without a client timeout a stalled FDC socket holds its thread for
        # the life of the process (usda-fdc >= 0.1.10).
        _fdc_client = FdcClient(  # reads FDC_API_KEY from env
            timeout=resilience.UPSTREAM_TIMEOUT_S,
        )
        _fdc_available = True
        logger.info("USDA FDC client initialized (API key configured).")
        return _fdc_client
    except (ValueError, ImportError) as e:
        _fdc_available = False
        logger.warning("USDA FDC client unavailable: %s", e)
        return None


async def _run_sync(func, *args, **kwargs) -> Any:
    """Run a blocking USDA call on this source's own thread pool.

    Not the default executor: a stalled USDA would hold threads that OFF then
    could not get. See resilience.make_executor.
    """
    return await resilience.run_in_executor(_executor, func, *args, **kwargs)


def is_available() -> bool:
    """Check if the USDA FDC client is configured and available."""
    return _get_fdc_client() is not None


async def search(query: str, page_size: int = 25) -> dict | None:
    """Search USDA FDC for foods matching a query string.

    Returns the raw SearchResult as a dict, or None if no client is
    configured. Upstream API errors propagate to the caller.
    """
    client = _get_fdc_client()
    if not client:
        return None

    ratelimit.spend(ratelimit.usda_limiter, "USDA_FDC")

    result = await _run_sync(client.search, query, page_size=page_size)
    return {
        "total_hits": result.total_hits,
        "foods": [
            {
                "fdc_id": f.fdc_id,
                "description": f.description,
                "data_type": f.data_type,
                "brand_owner": f.brand_owner,
                "brand_name": f.brand_name,
            }
            for f in result.foods
        ],
    }


async def get_food(fdc_id: int | str) -> dict | None:
    """Get detailed food data by FDC ID.

    Returns a dict with description, nutrients, brand info, etc., or None.
    """
    client = _get_fdc_client()
    if not client:
        return None

    stored = store.get(store.USDA_FOOD, fdc_id)
    if stored is not None:
        return stored

    ratelimit.spend(ratelimit.usda_limiter, "USDA_FDC")

    try:
        food = await _run_sync(client.get_food, fdc_id)
    except FdcResourceNotFoundError:
        # A food that does not exist is an answer, not an upstream failure.
        # Before usda-fdc 0.2.0 this arrived as an undifferentiated
        # FdcApiError, so five lookups of missing foods in a row would trip
        # the circuit breaker and shut USDA out entirely — punishing the
        # upstream for being asked about things that were never there.
        logger.info("USDA FDC has no food with id %s", fdc_id)
        return None

    record = {
        "fdc_id": food.fdc_id,
        "description": food.description,
        "data_type": food.data_type,
        "brand_owner": food.brand_owner,
        "brand_name": food.brand_name,
        "ingredients": food.ingredients,
        "serving_size": food.serving_size,
        "serving_size_unit": food.serving_size_unit,
        # A list, not a dict keyed by name: FDC publishes energy twice under
        # the identical name "Energy" (kcal id 1008, kJ id 1062), so a
        # name-keyed dict keeps whichever arrived last — and served cheddar at
        # 1710 kcal. Identity lives in the id.
        "nutrients": [
            {"id": n.id, "name": n.name, "amount": n.amount, "unit": n.unit_name}
            for n in food.nutrients
        ],
    }

    store.put(store.USDA_FOOD, fdc_id, record)
    return record


def normalize_gtin(gtin: str) -> str:
    """Normalize a barcode to GTIN-14 for comparison.

    GTIN-8/12/13/14 are the same identifier at different zero-paddings, and
    FDC is inconsistent about which it stores ("028400642255" vs
    "0099447210127"). Left-padding both sides to 14 digits makes padding
    variants compare equal. Returns "" if there are no digits, or if the
    barcode is longer than a GTIN-14.
    """
    digits = "".join(c for c in str(gtin) if c.isdigit())
    if not digits or len(digits) > 14:
        return ""
    return digits.zfill(14)


async def search_by_upc(upc: str) -> dict | None:
    """Search USDA FDC for a branded food by UPC/GTIN barcode.

    FDC exposes no barcode-lookup endpoint, so this queries the full-text
    search API. That search is fuzzy: an unknown barcode happily returns
    unrelated products (querying "00000000" yields a food whose real barcode
    is "0099447210127"). Taking the top hit on trust would attribute one
    product's nutrition to a different product's barcode — silent wrong data.

    So we verify each candidate's own gtin_upc against the requested barcode
    and return only a genuine match.

    Returns the matching food's full details, or None if nothing matches /
    no client is configured. Upstream API errors propagate to the caller.
    """
    client = _get_fdc_client()
    if not client:
        return None

    target = normalize_gtin(upc)
    if not target:
        return None

    # A barcode's FDC id does not change. Remembering it lets us skip the
    # *search* — the call that spends budget and that FDC answers fuzzily —
    # and go straight to the food.
    known_id = store.get(store.USDA_UPC, target)
    if known_id is not None:
        return await get_food(known_id)

    ratelimit.spend(ratelimit.usda_limiter, "USDA_FDC")

    result = await _run_sync(
        client.search, upc, data_type=["Branded"], page_size=10,
    )

    for food in result.foods:
        if normalize_gtin(food.gtin_upc or "") == target:
            store.put(store.USDA_UPC, target, food.fdc_id)
            return await get_food(food.fdc_id)

    logger.info("USDA FDC has no branded food matching GTIN %s", upc)
    return None


_probe = resilience.CachedProbe("USDA_FDC")


async def check_connectivity() -> dict:
    """Check if the USDA FDC API is reachable. For the health endpoint.

    Cached and charged to the outbound budget — see the note in
    open_food_facts. /health is polled every 60s and exempt from the inbound
    limiter, so an unbounded probe lets any caller amplify /health into
    unlimited upstream load. Also bounded by the upstream timeout, so a stalled
    upstream cannot hang the endpoint the platform uses to decide whether we
    are alive.
    """
    if not is_available():
        return {"status": "unconfigured", "detail": "FDC_API_KEY not set"}

    cached = _probe.fresh()
    if cached is not None:
        return cached

    timeout = resilience.UPSTREAM_TIMEOUT_S
    try:
        ratelimit.spend(ratelimit.usda_limiter, "USDA_FDC")
        result = await asyncio.wait_for(
            _run_sync(_get_fdc_client().search, "test", page_size=1),
            timeout,
        )
        return _probe.store({"status": "ok", "total_foods": result.total_hits})
    except ratelimit.RateLimitedError:
        stale = _probe.last_known()
        if stale is not None:
            return stale
        return {"status": "unknown", "detail": "rate budget exhausted; not probed"}
    except asyncio.TimeoutError:
        return _probe.store({"status": "error", "detail": f"timed out after {timeout}s"})
    except Exception as e:
        return _probe.store({"status": "error", "detail": str(e)})
