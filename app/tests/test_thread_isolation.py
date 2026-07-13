"""
Tests that a stalled upstream cannot starve a healthy one.

Both vendor SDKs are synchronous, so every call occupies a thread for its whole
duration. On the shared default executor (8 threads on a small box) a hung Open
Food Facts holds every thread, and a perfectly healthy USDA call then times out
waiting for one.

The circuit breakers cannot prevent that: the contention is *underneath* them,
in the thread pool. And asyncio.wait_for cancels only the await, never the
blocking call itself, so a stalled thread stays occupied until the SDK's own
socket gives up.

These tests use the real executors — mocking the upstreams away is exactly what
hid this failure mode from the existing breaker-isolation tests.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import open_food_facts as off
from app.core import resilience, usda_fdc


# ── each source owns a bounded pool ───────────────────────────────────

def test_each_upstream_has_its_own_executor():
    assert off._executor is not usda_fdc._executor
    assert isinstance(off._executor, ThreadPoolExecutor)
    assert isinstance(usda_fdc._executor, ThreadPoolExecutor)


def test_upstream_executors_are_not_the_default_pool():
    """None means asyncio's shared default executor — the thing we must avoid."""
    assert off._executor is not None
    assert usda_fdc._executor is not None


def test_executors_are_bounded():
    """An unbounded pool under a stalled upstream is a thread leak."""
    assert off._executor._max_workers == resilience.UPSTREAM_MAX_THREADS
    assert usda_fdc._executor._max_workers == resilience.UPSTREAM_MAX_THREADS
    assert resilience.UPSTREAM_MAX_THREADS > 0


def test_threads_are_named_for_their_source():
    """So a thread dump during an incident says which upstream is stuck."""
    assert off._executor._thread_name_prefix == "upstream-off"
    assert usda_fdc._executor._thread_name_prefix == "upstream-usda"


def test_make_executor_is_configurable():
    ex = resilience.make_executor("test")
    try:
        assert ex._max_workers == resilience.UPSTREAM_MAX_THREADS
        assert ex._thread_name_prefix == "upstream-test"
    finally:
        ex.shutdown(wait=False)


# ── the isolation itself ──────────────────────────────────────────────

async def test_a_stalled_upstream_does_not_starve_a_healthy_one():
    """The bug: saturating OFF's threads used to make a healthy USDA call time
    out, because both drew from the same default pool."""
    stall = 1.0

    def blocking_off_call(_):
        time.sleep(stall)          # OFF: socket open, no data coming

    def fast_usda_call(*_a, **_kw):
        return "usda-ok"           # USDA: perfectly healthy

    # Occupy every thread OFF is allowed to have
    stalled = [
        asyncio.create_task(off._run_sync(blocking_off_call, i))
        for i in range(resilience.UPSTREAM_MAX_THREADS)
    ]
    await asyncio.sleep(0.2)       # let them all claim their threads

    try:
        start = time.monotonic()
        result = await asyncio.wait_for(
            usda_fdc._run_sync(fast_usda_call, "test"), timeout=2.0,
        )
        elapsed = time.monotonic() - start

        assert result == "usda-ok"
        assert elapsed < 0.5, f"healthy USDA call waited {elapsed:.2f}s for a thread"
    finally:
        await asyncio.gather(*stalled, return_exceptions=True)


async def test_a_stalled_upstream_does_not_starve_the_health_probe():
    """/health is what the platform polls. It must not be blocked by the very
    upstream it is trying to report on."""
    def blocking_off_call(_):
        time.sleep(1.0)

    stalled = [
        asyncio.create_task(off._run_sync(blocking_off_call, i))
        for i in range(resilience.UPSTREAM_MAX_THREADS)
    ]
    await asyncio.sleep(0.2)

    try:
        class HealthyClient:
            def search(self, query, page_size=1):
                class R:
                    total_hits = 1
                return R()

        import unittest.mock as mock
        with mock.patch.object(usda_fdc, "_get_fdc_client", lambda: HealthyClient()):
            start = time.monotonic()
            status = await usda_fdc.check_connectivity()
            elapsed = time.monotonic() - start

        assert status["status"] == "ok"
        assert elapsed < 0.5
    finally:
        await asyncio.gather(*stalled, return_exceptions=True)


async def test_saturating_a_pool_queues_rather_than_spawning_threads():
    """Past the cap, calls queue. That bounds the damage from a stalled
    upstream to its own pool instead of the whole process."""
    def blocking(_):
        time.sleep(0.4)

    over_capacity = resilience.UPSTREAM_MAX_THREADS + 4
    tasks = [
        asyncio.create_task(off._run_sync(blocking, i))
        for i in range(over_capacity)
    ]
    await asyncio.sleep(0.1)

    try:
        live = {
            t.name for t in __import__("threading").enumerate()
            if t.name.startswith("upstream-off")
        }
        assert len(live) <= resilience.UPSTREAM_MAX_THREADS
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("wrapper", [off, usda_fdc])
async def test_run_sync_still_returns_values_and_propagates_errors(wrapper):
    assert await wrapper._run_sync(lambda x: x * 2, 21) == 42

    def boom():
        raise ConnectionError("upstream down")

    with pytest.raises(ConnectionError):
        await wrapper._run_sync(boom)


# ── the socket timeout: what actually releases a stuck thread ──────────

def test_the_fdc_client_is_built_with_a_socket_timeout(monkeypatch):
    """Dedicated pools *contain* a stalled upstream; only a socket timeout
    releases the thread.

    asyncio.wait_for cancels the await, never the blocking SDK call beneath
    it, so without a client timeout a stalled FDC socket held its thread for
    the life of the process — requests has no default timeout. usda-fdc 0.1.10
    added one; this pins that we actually set it.
    """
    captured = {}

    class FakeFdcClient:
        def __init__(self, timeout=None, **kwargs):
            captured["timeout"] = timeout

    monkeypatch.setattr(usda_fdc, "_fdc_client", None)
    monkeypatch.setattr(usda_fdc, "_fdc_available", None)
    monkeypatch.setattr("usda_fdc.FdcClient", FakeFdcClient)

    usda_fdc._get_fdc_client()

    assert captured["timeout"] == resilience.UPSTREAM_TIMEOUT_S
    assert captured["timeout"] > 0


def test_the_library_supports_a_timeout():
    """Canary: if usda_fdc drops the timeout parameter, threads leak again."""
    import inspect

    from usda_fdc import FdcClient

    assert "timeout" in inspect.signature(FdcClient.__init__).parameters
