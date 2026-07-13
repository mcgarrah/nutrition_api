"""
Rate limiting, in two directions.

**Outbound** is the one that matters most, and it is not about protecting us.
Open Food Facts allows **15 product reads per minute per IP** and says plainly:
"If these limits are reached, we reserve the right to deny you access to the
website and the API through IP address ban." USDA FDC reports its own ceiling in
an `x-ratelimit-limit` header: 3600/hour. One uncached lookup spends one call at
each. So a public endpoint with no outbound throttle is a public endpoint that
gets its server IP banned from Open Food Facts, and it does not take long.

**Inbound** protects the service itself, and keeps our upstream spend inside
that budget in the first place.

Both use a token bucket: a steady refill rate with a burst allowance, so normal
traffic passes untouched and only sustained excess is shed.

A caveat worth stating plainly: these buckets live in process memory, so with
`--workers N` each worker holds its own. The upstream budgets are therefore
divided by the worker count (see UPSTREAM_WORKERS) — the alternative is a shared
counter, which means Redis, which this design deliberately avoids.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import logging
import os
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class TokenBucket:
    """Allow `rate` events per `per` seconds, tolerating a burst of `burst`."""

    def __init__(self, rate: float, per: float = 60.0, burst: float | None = None,
                 timer=time.monotonic):
        self.rate = float(rate)
        self.per = float(per)
        self.capacity = float(burst if burst is not None else rate)
        self._tokens = self.capacity
        self._timer = timer
        self._updated = timer()

    def _refill(self) -> None:
        now = self._timer()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(
            self.capacity,
            self._tokens + elapsed * (self.rate / self.per),
        )
        self._updated = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Take a token if one is available. Never blocks."""
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, cost: float = 1.0) -> float:
        """Seconds until `cost` tokens will be available."""
        self._refill()
        missing = cost - self._tokens
        if missing <= 0:
            return 0.0
        return missing / (self.rate / self.per)


class KeyedRateLimiter:
    """One bucket per key (a client IP), with a bounded number of keys.

    The bound matters: an unbounded dict keyed on client IP is a memory leak
    that any caller can drive, which is a poor trade for a component whose job
    is to make the service harder to abuse.
    """

    def __init__(self, rate: float, per: float = 60.0, burst: float | None = None,
                 max_keys: int = 10_000, timer=time.monotonic):
        self.rate = rate
        self.per = per
        self.burst = burst
        self.max_keys = max_keys
        self._timer = timer
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def _bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._buckets.popitem(last=False)  # evict least recently used
            bucket = TokenBucket(self.rate, self.per, self.burst, self._timer)
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
        return bucket

    def try_acquire(self, key: str) -> bool:
        return self._bucket(key).try_acquire()

    def retry_after(self, key: str) -> float:
        return self._bucket(key).retry_after()

    def __len__(self) -> int:
        return len(self._buckets)


# ── Configuration ─────────────────────────────────────────────────────

# Worker processes sharing this deployment's single outbound IP. The upstream
# budgets below are per-IP, so each worker may only spend its share of them.
UPSTREAM_WORKERS = int(os.environ.get("UPSTREAM_WORKERS", "2"))

# Open Food Facts: 15 product reads/minute per IP, enforced with an IP ban.
OFF_RATE_PER_MIN = float(os.environ.get("OFF_RATE_PER_MIN", "15"))

# USDA FDC: reports x-ratelimit-limit: 3600 per hour.
USDA_RATE_PER_MIN = float(os.environ.get("USDA_RATE_PER_MIN", "60"))

# Inbound, per client IP. Generous next to the upstream budgets because the
# cache absorbs repeats — only distinct, uncached barcodes cost an upstream call.
INBOUND_RATE_PER_MIN = float(os.environ.get("INBOUND_RATE_PER_MIN", "60"))
INBOUND_BURST = float(os.environ.get("INBOUND_BURST", "20"))


def _per_worker(total: float) -> float:
    """Split a per-IP upstream budget across the worker processes."""
    return max(total / max(UPSTREAM_WORKERS, 1), 1.0)


# Outbound: one bucket per upstream, sized to this worker's share of the budget.
off_limiter = TokenBucket(rate=_per_worker(OFF_RATE_PER_MIN), per=60.0)
usda_limiter = TokenBucket(rate=_per_worker(USDA_RATE_PER_MIN), per=60.0)

# Inbound: one bucket per client IP.
#
# Divided across workers for the same reason the upstream budgets are. A
# client's requests are load-balanced across worker processes, and each worker
# holds its own bucket — so an inbound limit that ignored the worker count
# would let through N times what it advertised. Measured: 30 rapid requests
# against a nominal burst of 20 produced zero 429s with two workers.
inbound_limiter = KeyedRateLimiter(
    rate=_per_worker(INBOUND_RATE_PER_MIN),
    per=60.0,
    burst=_per_worker(INBOUND_BURST),
)
