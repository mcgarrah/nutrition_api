"""
Tests for rate limiting, in both directions.

The outbound half is the one that decides whether this service can be public at
all. Open Food Facts allows **15 product reads per minute per IP** and says
plainly: "we reserve the right to deny you access to the website and the API
through IP address ban." One uncached lookup spends one OFF call. So an
unthrottled public endpoint is an endpoint that gets the deployment's IP banned,
and it does not take many minutes.

Time is driven by an injected clock, so these are fast and deterministic.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import orchestrator, ratelimit, resilience, usda_fdc
from app.core.ratelimit import KeyedRateLimiter, TokenBucket
from app.main import app

client = TestClient(app)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ══ TokenBucket ═══════════════════════════════════════════════════════

def test_bucket_allows_up_to_its_burst_immediately():
    clock = FakeClock()
    bucket = TokenBucket(rate=15, per=60.0, timer=clock)

    assert all(bucket.try_acquire() for _ in range(15))
    assert bucket.try_acquire() is False


def test_bucket_refills_at_the_configured_rate():
    clock = FakeClock()
    bucket = TokenBucket(rate=60, per=60.0, timer=clock)   # one per second

    for _ in range(60):
        bucket.try_acquire()
    assert bucket.try_acquire() is False

    clock.advance(1.0)
    assert bucket.try_acquire() is True     # exactly one token back
    assert bucket.try_acquire() is False


def test_bucket_does_not_refill_beyond_capacity():
    """An idle hour must not bank an hour's worth of burst."""
    clock = FakeClock()
    bucket = TokenBucket(rate=15, per=60.0, timer=clock)

    clock.advance(3600)
    assert bucket.tokens == 15              # capped at capacity, not 900


def test_bucket_burst_can_exceed_the_steady_rate():
    clock = FakeClock()
    bucket = TokenBucket(rate=10, per=60.0, burst=25, timer=clock)

    assert sum(bucket.try_acquire() for _ in range(25)) == 25
    assert bucket.try_acquire() is False


def test_retry_after_reports_when_a_token_returns():
    clock = FakeClock()
    bucket = TokenBucket(rate=60, per=60.0, timer=clock)   # one per second

    for _ in range(60):
        bucket.try_acquire()

    assert bucket.retry_after() == pytest.approx(1.0, abs=0.01)
    assert TokenBucket(rate=60, per=60.0, timer=clock).retry_after() == 0.0


# ══ KeyedRateLimiter ══════════════════════════════════════════════════

def test_each_client_gets_its_own_budget():
    clock = FakeClock()
    limiter = KeyedRateLimiter(rate=5, per=60.0, timer=clock)

    for _ in range(5):
        assert limiter.try_acquire("1.2.3.4")
    assert limiter.try_acquire("1.2.3.4") is False

    # A different caller is unaffected by the first one's excess
    assert limiter.try_acquire("5.6.7.8") is True


def test_keys_are_bounded_so_the_limiter_is_not_itself_a_memory_leak():
    """An unbounded dict keyed on client IP is a leak any caller can drive —
    a poor trade for a component whose job is to make abuse harder."""
    clock = FakeClock()
    limiter = KeyedRateLimiter(rate=5, per=60.0, max_keys=10, timer=clock)

    for i in range(50):
        limiter.try_acquire(f"10.0.0.{i}")

    assert len(limiter) == 10


# ══ Outbound: staying inside the upstream budgets ═════════════════════

@pytest.fixture
def unlimited(monkeypatch):
    """Reset the shared outbound buckets between tests."""
    clock = FakeClock()

    def install(off_rate=1000, usda_rate=1000):
        monkeypatch.setattr(
            ratelimit, "off_limiter",
            TokenBucket(rate=off_rate, per=60.0, timer=clock),
        )
        monkeypatch.setattr(
            ratelimit, "usda_limiter",
            TokenBucket(rate=usda_rate, per=60.0, timer=clock),
        )
        return clock

    return install


async def test_off_is_skipped_once_its_budget_is_spent(monkeypatch, unlimited, gpc_db):
    """Over-spending OFF's budget is not a failed request — it is an IP ban."""
    unlimited(off_rate=2)
    calls = []

    async def off_counting(barcode):
        calls.append(barcode)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_counting)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    for i in range(5):
        await orchestrator.lookup(f"11111111000{i}")

    assert len(calls) == 2          # the budget, and not one call more


async def test_a_spent_budget_degrades_rather_than_failing(monkeypatch, unlimited, gpc_db):
    """USDA still answers; the response is partial, not an error."""
    unlimited(off_rate=0.0001)

    async def off_never_called(barcode):
        raise AssertionError("OFF was called despite an exhausted budget")

    async def usda_ok(upc):
        return {
            "description": "COLA",
            "nutrients": [
                {"id": 1008, "name": "Energy", "amount": 42.0, "unit": "KCAL"}
            ],
        }

    monkeypatch.setattr(off, "get_product", off_never_called)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_ok)

    product = await orchestrator.lookup("028400642255")

    assert product.data_sources == ["USDA_FDC"]
    assert product.calories_kcal == 42.0


async def test_a_spent_budget_does_not_trip_the_circuit_breaker(
    monkeypatch, unlimited, gpc_db,
):
    """The call was never made. Our own budget running dry says nothing about
    the upstream's health, and must not be recorded as its failure — otherwise
    a busy minute would open the circuit and keep OFF shut out long after the
    budget refilled."""
    unlimited(off_rate=0.0001)

    async def off_never_called(barcode):
        raise AssertionError("should not be called")

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_never_called)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    for i in range(resilience.off_breaker.failure_threshold + 3):
        await orchestrator.lookup(f"22222222000{i}")

    assert not resilience.off_breaker.is_open
    assert resilience.off_breaker._consecutive_failures == 0


async def test_the_budget_refills(monkeypatch, unlimited, gpc_db):
    clock = unlimited(off_rate=60)      # one per second
    calls = []

    async def off_counting(barcode):
        calls.append(barcode)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_counting)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    for i in range(60):
        await orchestrator.lookup(f"3333333300{i:02d}")
    assert len(calls) == 60

    await orchestrator.lookup("444444444444")
    assert len(calls) == 60              # budget spent

    clock.advance(5)
    await orchestrator.lookup("555555555555")
    assert len(calls) == 61              # refilled


async def test_a_cache_hit_costs_no_upstream_budget(monkeypatch, unlimited, gpc_db):
    """The cache is what makes the 15/minute budget survivable: repeat scans of
    the same barcode must not spend a token."""
    unlimited(off_rate=2)
    calls = []

    async def off_counting(barcode):
        calls.append(barcode)
        return {"product_name": "Cola", "nutrients_per_100g": {}, "categories": []}

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_counting)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    for _ in range(20):
        await orchestrator.lookup("028400642255")

    assert len(calls) == 1


# ══ Configuration matches what the upstreams actually publish ═════════

def test_off_budget_matches_open_food_facts_documented_limit():
    """15 product reads/minute per IP, enforced with an IP ban."""
    assert ratelimit.OFF_RATE_PER_MIN == 15


def test_usda_budget_matches_the_rate_limit_header():
    """FDC reports x-ratelimit-limit: 3600 per hour."""
    assert ratelimit.USDA_RATE_PER_MIN == 60


def test_upstream_budgets_are_divided_across_workers():
    """The limits are per *IP*, but each worker holds its own bucket. Two
    workers each spending the full budget would spend twice the budget."""
    assert ratelimit.UPSTREAM_WORKERS >= 1
    assert ratelimit.off_limiter.rate <= ratelimit.OFF_RATE_PER_MIN
    assert ratelimit.usda_limiter.rate <= ratelimit.USDA_RATE_PER_MIN


# ══ Inbound ═══════════════════════════════════════════════════════════

@pytest.fixture
def inbound(monkeypatch):
    clock = FakeClock()

    def install(rate=5, burst=None):
        monkeypatch.setattr(
            ratelimit, "inbound_limiter",
            KeyedRateLimiter(rate=rate, per=60.0, burst=burst, timer=clock),
        )
        return clock

    return install


def test_excess_requests_get_429(inbound, gpc_db):
    inbound(rate=3)

    codes = [client.get("/api/gpc/segments/").status_code for _ in range(6)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429, 429]


def test_429_carries_a_retry_after_header(inbound, gpc_db):
    inbound(rate=1)

    client.get("/api/gpc/segments/")
    resp = client.get("/api/gpc/segments/")

    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert "slow down" in resp.json()["detail"].lower()


def test_health_is_never_rate_limited(inbound, gpc_db):
    """The platform polls /health. A 429 there reads as unhealthy, and the rate
    limiter would get the container restarted."""
    inbound(rate=1)

    for _ in range(10):
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/version", "/docs", "/openapi.json", "/"])
def test_static_and_ops_paths_are_exempt(path, inbound, gpc_db):
    inbound(rate=1)

    for _ in range(5):
        assert client.get(path).status_code == 200


def test_the_limit_is_per_client(inbound, gpc_db):
    inbound(rate=2)

    for _ in range(2):
        assert client.get(
            "/api/gpc/segments/", headers={"X-Forwarded-For": "1.1.1.1"},
        ).status_code == 200

    assert client.get(
        "/api/gpc/segments/", headers={"X-Forwarded-For": "1.1.1.1"},
    ).status_code == 429

    # A different caller is unaffected
    assert client.get(
        "/api/gpc/segments/", headers={"X-Forwarded-For": "2.2.2.2"},
    ).status_code == 200


def test_the_first_forwarded_hop_identifies_the_client(inbound, gpc_db):
    """Behind a proxy the socket address is the proxy's, so X-Forwarded-For is
    what distinguishes callers."""
    inbound(rate=1)

    headers = {"X-Forwarded-For": "9.9.9.9, 10.0.0.1, 172.16.0.1"}
    assert client.get("/api/gpc/segments/", headers=headers).status_code == 200
    assert client.get("/api/gpc/segments/", headers=headers).status_code == 429

    # Same proxy chain, different origin client -> its own budget
    other = {"X-Forwarded-For": "8.8.8.8, 10.0.0.1, 172.16.0.1"}
    assert client.get("/api/gpc/segments/", headers=other).status_code == 200


def test_the_inbound_limit_is_also_divided_across_workers():
    """A client's requests are load-balanced across worker processes, and each
    worker holds its own bucket — so a limit that ignored the worker count
    would let through N times what it advertised.

    Measured live before this was fixed: 30 rapid requests against a nominal
    burst of 20 produced zero 429s with two workers.
    """
    assert ratelimit.inbound_limiter.rate <= ratelimit.INBOUND_RATE_PER_MIN
    assert ratelimit.inbound_limiter.burst <= ratelimit.INBOUND_BURST

    if ratelimit.UPSTREAM_WORKERS > 1:
        assert ratelimit.inbound_limiter.rate < ratelimit.INBOUND_RATE_PER_MIN
