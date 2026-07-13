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
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Awaitable, Callable

from usda_fdc.exceptions import FdcRateLimitError, FdcResourceNotFoundError

from .ratelimit import RateLimitedError

logger = logging.getLogger(__name__)

# Max seconds we wait on any single upstream call (CLAUDE.md: max 2.0s)
UPSTREAM_TIMEOUT_S = float(os.environ.get("UPSTREAM_TIMEOUT_S", "2.0"))

# Threads reserved for each upstream. Both vendor SDKs are synchronous, so
# every call occupies a thread for its whole duration.
UPSTREAM_MAX_THREADS = int(os.environ.get("UPSTREAM_MAX_THREADS", "8"))


def make_executor(name: str) -> ThreadPoolExecutor:
    """Create a bounded thread pool dedicated to one upstream.

    The vendor SDKs are blocking, so they run in an executor. Sharing asyncio's
    default executor means a stalled upstream holds threads that a *healthy*
    one then cannot get: with the default pool of 8, a hung Open Food Facts
    starves USDA, and a perfectly good source times out waiting for a thread.

    The circuit breakers cannot prevent that — the contention is underneath
    them. asyncio.wait_for also cancels only the await, never the blocking call
    itself, so a stalled thread stays occupied until the SDK's own socket gives
    up. Giving each source its own bounded pool contains the damage to the
    source that caused it.
    """
    return ThreadPoolExecutor(
        max_workers=UPSTREAM_MAX_THREADS,
        thread_name_prefix=f"upstream-{name}",
    )


async def run_in_executor(executor: ThreadPoolExecutor, func, *args, **kwargs) -> Any:
    """Run a blocking callable on a specific (per-source) executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(func, *args, **kwargs))


# How long a health probe's verdict stays good for.
#
# /health is polled by the platform every 60s and is exempt from the inbound
# rate limiter — it has to be, since a 429 there reads as "unhealthy" and gets
# the container restarted. But an unbounded probe turns that exemption into an
# amplifier: every poll made a live call to Open Food Facts, so any caller could
# loop /health and drive unlimited traffic at a nonprofit's API through us. Open
# Food Facts' stated remedy for that is an IP ban.
HEALTH_PROBE_TTL_S = float(os.environ.get("HEALTH_PROBE_TTL_S", "60"))


class CachedProbe:
    """Remember an upstream's last verdict for a while.

    Bounds probe traffic to at most one call per TTL no matter how often
    /health is polled, and gives us a last-known answer to serve when the
    budget is spent rather than either lying or spending a token we don't have.
    """

    def __init__(self, name: str, ttl_s: float = HEALTH_PROBE_TTL_S,
                 timer=time.monotonic):
        self.name = name
        self.ttl_s = ttl_s
        self._timer = timer
        self._result: dict | None = None
        self._at = 0.0

    def fresh(self) -> dict | None:
        """The cached verdict, if it hasn't gone stale."""
        if self._result is None:
            return None
        if self._timer() - self._at >= self.ttl_s:
            return None
        return dict(self._result)

    def last_known(self) -> dict | None:
        """The cached verdict even if stale — better than no answer at all."""
        return dict(self._result) if self._result is not None else None

    def store(self, result: dict) -> dict:
        self._result = dict(result)
        self._at = self._timer()
        return result

    def clear(self) -> None:
        self._result = None
        self._at = 0.0


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

    async def call(
        self,
        coro_fn: Callable[[], Awaitable[Any]],
        timeout: float | None = None,
    ) -> Any:
        """Run an async callable through the breaker with a timeout.

        `timeout` defaults to UPSTREAM_TIMEOUT_S, which bounds a single upstream
        call. An operation that makes *several* round trips needs a budget for
        each of them: a USDA barcode lookup is a search followed by a fetch, and
        holding the pair to one call's allowance timed it out and dropped USDA
        from the response — intermittently, depending on how quick FDC felt.

        Raises CircuitOpenError when the circuit is open, TimeoutError when the
        allowance is exceeded. A timeout counts as a failure; a rate limit and a
        "not found" do not.
        """
        if self.is_open:
            raise CircuitOpenError(f"{self.name} circuit is open")
        budget = UPSTREAM_TIMEOUT_S if timeout is None else timeout
        try:
            result = await asyncio.wait_for(coro_fn(), timeout=budget)
        except (RateLimitedError, FdcRateLimitError):
            # A rate limit — ours or theirs — is a budgeting fact, not an
            # outage. Recording it as a failure would trip the circuit and keep
            # the source shut out long after the limit had reset, punishing the
            # upstream for our own busy minute.
            raise
        except FdcResourceNotFoundError:
            # The upstream answered, and its answer was "no such thing". That
            # is a healthy API doing its job, not a failing one.
            raise
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


# One breaker per upstream source, shared across requests
usda_breaker = CircuitBreaker("USDA_FDC")
off_breaker = CircuitBreaker("OpenFoodFacts")
