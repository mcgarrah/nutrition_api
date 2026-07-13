"""
Tests for the circuit breaker and upstream timeout handling.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio

import pytest

from app.core import resilience
from app.core.resilience import CircuitBreaker, CircuitOpenError


async def test_breaker_opens_after_threshold_failures():
    breaker = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)

    async def failing():
        raise ConnectionError("upstream down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(lambda: failing())

    assert breaker.is_open
    with pytest.raises(CircuitOpenError):
        await breaker.call(lambda: failing())


async def test_breaker_half_open_probe_closes_on_success():
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_s=60)

    async def failing():
        raise ConnectionError("upstream down")

    async def working():
        return "ok"

    with pytest.raises(ConnectionError):
        await breaker.call(lambda: failing())
    assert breaker.is_open

    # Simulate cooldown expiry — breaker becomes half-open
    breaker._opened_at -= 61
    assert not breaker.is_open

    result = await breaker.call(lambda: working())
    assert result == "ok"
    assert not breaker.is_open
    assert breaker._consecutive_failures == 0


async def test_breaker_success_resets_failure_count():
    breaker = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)

    async def failing():
        raise ConnectionError("boom")

    async def working():
        return "ok"

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await breaker.call(lambda: failing())
    await breaker.call(lambda: working())

    assert breaker._consecutive_failures == 0
    assert not breaker.is_open


async def test_slow_call_times_out_and_counts_as_failure(monkeypatch):
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.05)
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_s=60)

    async def slow():
        await asyncio.sleep(1.0)

    with pytest.raises(asyncio.TimeoutError):
        await breaker.call(lambda: slow())
    assert breaker.is_open
