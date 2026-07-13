"""
Tests for the behaviours line coverage cannot see: that upstream calls really
run concurrently, that the TTL cache actually expires and evicts, and that a
tripped circuit breaker really recovers once its cooldown elapses.

Time is driven by injected clocks rather than sleeps, so these stay fast and
deterministic.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import time

import pytest
from cachetools import TTLCache

from app.core import open_food_facts as off
from app.core import orchestrator, resilience, usda_fdc
from app.core.resilience import CircuitBreaker, CircuitOpenError


class FakeClock:
    """A hand-cranked monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ══ Concurrency ═══════════════════════════════════════════════════════

async def test_upstreams_are_queried_in_parallel_not_in_series(monkeypatch, gpc_db):
    """The whole point of asyncio.gather: worst-case latency is the slowest
    upstream, not the sum of them."""
    delay = 0.15

    async def slow_off(barcode):
        await asyncio.sleep(delay)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def slow_usda(upc):
        await asyncio.sleep(delay)
        return {"description": "COLA", "nutrients": {}}

    monkeypatch.setattr(off, "get_product", slow_off)
    monkeypatch.setattr(usda_fdc, "search_by_upc", slow_usda)

    start = time.monotonic()
    product = await orchestrator.lookup("028400642255")
    elapsed = time.monotonic() - start

    assert set(product.data_sources) >= {"OpenFoodFacts", "USDA_FDC"}
    # Serial would be >= 2 * delay; allow generous headroom for slow CI
    assert elapsed < delay * 1.8, f"took {elapsed:.3f}s — upstreams ran serially"


async def test_a_slow_upstream_does_not_delay_a_fast_one(monkeypatch, gpc_db):
    """OFF must not be held hostage by a USDA call that will time out."""
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.1)

    async def fast_off(barcode):
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def hanging_usda(upc):
        await asyncio.sleep(10)

    monkeypatch.setattr(off, "get_product", fast_off)
    monkeypatch.setattr(usda_fdc, "search_by_upc", hanging_usda)

    start = time.monotonic()
    product = await orchestrator.lookup("028400642255")
    elapsed = time.monotonic() - start

    assert product.data_sources == ["OpenFoodFacts"]
    assert elapsed < 1.0            # bounded by the timeout, not the 10s sleep


async def test_concurrent_lookups_of_the_same_gtin_all_succeed(monkeypatch, gpc_db):
    """Ten simultaneous scans of the same barcode must not corrupt the cache."""
    async def off_ok(barcode):
        await asyncio.sleep(0.01)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_ok)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    results = await asyncio.gather(
        *(orchestrator.lookup("028400642255") for _ in range(10))
    )

    assert all(r.product_name == "Cola" for r in results)
    assert all(r.gtin == "028400642255" for r in results)


async def test_concurrent_lookups_of_different_gtins_do_not_cross_contaminate(
    monkeypatch, gpc_db,
):
    async def off_by_barcode(barcode):
        return {
            "product_name": f"Product-{barcode}",
            "nutrients_per_100g": {},
            "categories": [],
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_by_barcode)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    gtins = [f"1234567890{i:03d}" for i in range(8)]
    results = await asyncio.gather(*(orchestrator.lookup(g) for g in gtins))

    for gtin, product in zip(gtins, results):
        assert product.gtin == gtin
        assert product.product_name == f"Product-{gtin}"


# ══ Cache expiry and eviction ═════════════════════════════════════════

@pytest.fixture
def clocked_cache(monkeypatch):
    """Swap in a lookup cache whose TTL is driven by a fake clock."""
    clock = FakeClock()

    def install(maxsize=128, ttl=300):
        cache = TTLCache(maxsize=maxsize, ttl=ttl, timer=clock)
        monkeypatch.setattr(orchestrator, "_lookup_cache", cache)
        return cache

    install.clock = clock
    return install


def counting_sources(monkeypatch, calls):
    async def off_counting(barcode):
        calls.append(barcode)
        return {
            "product_name": f"Product-{barcode}",
            "nutrients_per_100g": {},
            "categories": [],
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_counting)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)


async def test_cache_serves_a_hit_without_touching_upstreams(
    monkeypatch, clocked_cache, gpc_db,
):
    clocked_cache()
    calls = []
    counting_sources(monkeypatch, calls)

    await orchestrator.lookup("028400642255")
    await orchestrator.lookup("028400642255")

    assert calls == ["028400642255"]        # upstream hit exactly once


async def test_cache_entry_expires_after_its_ttl(monkeypatch, clocked_cache, gpc_db):
    """Food data is static, but not immortal — a stale entry must lapse."""
    install = clocked_cache
    install(ttl=300)
    calls = []
    counting_sources(monkeypatch, calls)

    await orchestrator.lookup("028400642255")
    install.clock.advance(299)
    await orchestrator.lookup("028400642255")
    assert len(calls) == 1                  # still inside the TTL

    install.clock.advance(2)                # now past 300s
    await orchestrator.lookup("028400642255")
    assert len(calls) == 2                  # refetched


async def test_cache_evicts_when_full(monkeypatch, clocked_cache, gpc_db):
    """A bounded cache must not grow without limit under barcode churn."""
    cache = clocked_cache(maxsize=2)
    calls = []
    counting_sources(monkeypatch, calls)

    await orchestrator.lookup("111111111111")
    await orchestrator.lookup("222222222222")
    await orchestrator.lookup("333333333333")   # evicts one of the first two

    assert len(cache) == 2
    assert len(calls) == 3


async def test_eviction_does_not_corrupt_surviving_entries(
    monkeypatch, clocked_cache, gpc_db,
):
    clocked_cache(maxsize=2)
    calls = []
    counting_sources(monkeypatch, calls)

    await orchestrator.lookup("111111111111")
    await orchestrator.lookup("222222222222")
    await orchestrator.lookup("333333333333")

    survivor = await orchestrator.lookup("333333333333")
    assert survivor.product_name == "Product-333333333333"


async def test_expired_entry_is_refetched_not_served_stale(
    monkeypatch, clocked_cache, gpc_db,
):
    """After expiry the *new* upstream value must win, not the cached one."""
    install = clocked_cache
    install(ttl=10)
    name = {"value": "Original"}

    async def off_changing(barcode):
        return {
            "product_name": name["value"],
            "nutrients_per_100g": {},
            "categories": [],
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_changing)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    first = await orchestrator.lookup("028400642255")
    assert first.product_name == "Original"

    name["value"] = "Renamed"
    install.clock.advance(11)

    second = await orchestrator.lookup("028400642255")
    assert second.product_name == "Renamed"


# ══ Circuit breaker recovery over time ════════════════════════════════

async def test_breaker_reopens_if_the_probe_fails_again(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(resilience.time, "monotonic", clock)
    breaker = CircuitBreaker("test", failure_threshold=2, cooldown_s=60)

    async def failing():
        raise ConnectionError("still down")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await breaker.call(failing)
    assert breaker.is_open

    clock.advance(61)                       # cooldown elapsed -> half-open
    assert not breaker.is_open

    with pytest.raises(ConnectionError):    # the probe fails
        await breaker.call(failing)

    assert breaker.is_open                  # slammed shut again
    with pytest.raises(CircuitOpenError):
        await breaker.call(failing)


async def test_breaker_cooldown_restarts_from_the_failed_probe(monkeypatch):
    """A failed probe must reset the clock, not inherit the original open time."""
    clock = FakeClock()
    monkeypatch.setattr(resilience.time, "monotonic", clock)
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_s=60)

    async def failing():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await breaker.call(failing)

    clock.advance(61)
    with pytest.raises(ConnectionError):    # probe fails, re-opening at t=1061
        await breaker.call(failing)

    clock.advance(30)                       # only 30s since the re-open
    assert breaker.is_open

    clock.advance(31)                       # now 61s since the re-open
    assert not breaker.is_open


async def test_recovered_upstream_is_used_again(monkeypatch, gpc_db):
    """The full round trip: USDA fails, trips the breaker, then recovers."""
    clock = FakeClock()
    monkeypatch.setattr(resilience.time, "monotonic", clock)

    state = {"healthy": False}

    async def off_ok(barcode):
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def flaky_usda(upc):
        if not state["healthy"]:
            raise ConnectionError("FDC down")
        return {"description": "COLA", "nutrients": {"Energy": {"amount": 42.0}}}

    monkeypatch.setattr(off, "get_product", off_ok)
    monkeypatch.setattr(usda_fdc, "search_by_upc", flaky_usda)

    for i in range(resilience.usda_breaker.failure_threshold):
        product = await orchestrator.lookup(f"11111111111{i}")
        assert "USDA_FDC" not in product.data_sources
    assert resilience.usda_breaker.is_open

    state["healthy"] = True
    clock.advance(resilience.usda_breaker.cooldown_s + 1)

    product = await orchestrator.lookup("999999999999")
    assert "USDA_FDC" in product.data_sources
    assert product.calories_kcal == 42.0


# ══ Latency telemetry ═════════════════════════════════════════════════

async def test_reported_latency_tracks_actual_upstream_time(monkeypatch, gpc_db):
    async def slow_off(barcode):
        await asyncio.sleep(0.05)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def instant_usda(upc):
        return None

    monkeypatch.setattr(off, "get_product", slow_off)
    monkeypatch.setattr(usda_fdc, "search_by_upc", instant_usda)

    product = await orchestrator.lookup("028400642255")

    assert product.upstream_latency_ms["OpenFoodFacts"] >= 50
    assert product.upstream_latency_ms["USDA_FDC"] < 50
