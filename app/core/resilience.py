"""
Resilience primitives for upstream API calls.

Provides a per-source circuit breaker and a timeout wrapper so a slow or
failing upstream degrades gracefully instead of dragging down every request.

Circuit breaker states:
  CLOSED    — normal operation, calls pass through.
  OPEN      — too many consecutive failures; calls are skipped entirely
              until the cooldown expires.
  HALF-OPEN — cooldown expired; the next call is allowed through as a probe.
              Success closes the circuit, failure re-opens it.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Max seconds we wait on any single upstream call (CLAUDE.md: max 2.0s)
UPSTREAM_TIMEOUT_S = float(os.environ.get("UPSTREAM_TIMEOUT_S", "2.0"))


class CircuitOpenError(Exception):
    """Raised when a call is skipped because the circuit is open."""


class CircuitBreaker:
    """Consecutive-failure circuit breaker for one upstream source."""

    def __init__(self, name: str, failure_threshold: int = 5, cooldown_s: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_s:
            # Cooldown expired — half-open: allow a probe call through
            return False
        return True

    def record_success(self):
        if self._opened_at is not None:
            logger.info("Circuit %s closed after successful probe.", self.name)
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            if self._opened_at is None:
                logger.warning(
                    "Circuit %s opened after %d consecutive failures; "
                    "skipping calls for %.0fs.",
                    self.name, self._consecutive_failures, self.cooldown_s,
                )
            self._opened_at = time.monotonic()

    async def call(self, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run an async callable through the breaker with a timeout.

        Raises CircuitOpenError when the circuit is open, TimeoutError
        when the call exceeds UPSTREAM_TIMEOUT_S. Any exception (including
        timeout) counts as a failure.
        """
        if self.is_open:
            raise CircuitOpenError(f"{self.name} circuit is open")
        try:
            result = await asyncio.wait_for(coro_fn(), timeout=UPSTREAM_TIMEOUT_S)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


# One breaker per upstream source, shared across requests
usda_breaker = CircuitBreaker("USDA_FDC")
off_breaker = CircuitBreaker("OpenFoodFacts")
