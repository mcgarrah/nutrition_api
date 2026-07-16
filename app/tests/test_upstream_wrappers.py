"""
Tests for the USDA FDC and Open Food Facts service wrappers.

Both wrap synchronous vendor SDKs and bridge them onto the event loop. The
contract they owe the orchestrator is narrow but strict:

  * return None  → "no client configured" or "product genuinely not found"
  * raise        → the upstream failed (so the circuit breaker can see it)

Conflating those two is what makes a degraded upstream look like a missing
product, so most of these tests pin that boundary.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import time

import pytest

from app.core import open_food_facts as off
from app.core import resilience, usda_fdc


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch):
    """The wrappers memoize their clients in module globals."""
    monkeypatch.setattr(usda_fdc, "_fdc_client", None)
    monkeypatch.setattr(usda_fdc, "_fdc_available", None)
    monkeypatch.setattr(off, "_off_api", None)


# ══ USDA FDC ══════════════════════════════════════════════════════════

class _FakeNutrient:
    def __init__(self, name, amount, unit_name, id=1008):
        self.id = id
        self.name, self.amount, self.unit_name = name, amount, unit_name


class _FakeFood:
    fdc_id = 123
    description = "COLA"
    data_type = "Branded"
    brand_owner = "Coca-Cola Co"
    brand_name = "Coke"
    ingredients = "CARBONATED WATER"
    serving_size = 355.0
    serving_size_unit = "ml"
    food_category = "Soda"
    nutrients = [_FakeNutrient("Energy", 42.0, "KCAL")]


class _FakeSearchResult:
    total_hits = 1
    foods = [_FakeFood()]


# ── client bootstrap ──────────────────────────────────────────────────

def test_client_unavailable_without_api_key(monkeypatch):
    """No FDC_API_KEY: degrade quietly rather than crash the app."""
    def no_key(*a, **kw):
        raise ValueError("No API key provided")

    monkeypatch.setattr("usda_fdc.FdcClient", no_key)
    assert usda_fdc._get_fdc_client() is None
    assert usda_fdc.is_available() is False


def test_missing_key_is_remembered_not_retried(monkeypatch):
    """The failed bootstrap must be cached, not retried on every request."""
    calls = {"n": 0}

    def no_key(*a, **kw):
        calls["n"] += 1
        raise ValueError("No API key provided")

    monkeypatch.setattr("usda_fdc.FdcClient", no_key)
    usda_fdc._get_fdc_client()
    usda_fdc._get_fdc_client()
    assert calls["n"] == 1


def test_client_is_a_singleton(monkeypatch):
    monkeypatch.setattr("usda_fdc.FdcClient", lambda *a, **kw: _FakeSearchResult())
    first = usda_fdc._get_fdc_client()
    assert usda_fdc._get_fdc_client() is first
    assert usda_fdc.is_available() is True


# ── search / get_food ─────────────────────────────────────────────────

async def test_search_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: None)
    assert await usda_fdc.search("cola") is None


async def test_search_flattens_the_sdk_result(monkeypatch):
    class Client:
        def search(self, query, page_size=25):
            return _FakeSearchResult()

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    result = await usda_fdc.search("cola")

    assert result["total_hits"] == 1
    assert result["foods"][0] == {
        "fdc_id": 123, "description": "COLA", "data_type": "Branded",
        "brand_owner": "Coca-Cola Co", "brand_name": "Coke",
    }


async def test_search_propagates_upstream_errors(monkeypatch):
    """Must raise, not return None — the breaker needs to see the failure."""
    class Client:
        def search(self, query, page_size=25):
            raise ConnectionError("FDC down")

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    with pytest.raises(ConnectionError):
        await usda_fdc.search("cola")


async def test_get_food_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: None)
    assert await usda_fdc.get_food(123) is None


async def test_get_food_keeps_nutrient_ids(monkeypatch):
    """Identity lives in the id, not the name: FDC publishes energy twice under
    the identical name "Energy" (kcal 1008, kJ 1062), so a name-keyed dict
    would silently keep whichever arrived last."""
    class Client:
        def get_food(self, fdc_id):
            return _FakeFood()

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    food = await usda_fdc.get_food(123)

    assert food["description"] == "COLA"
    assert food["serving_size"] == 355.0
    assert food["nutrients"] == [
        {"id": 1008, "name": "Energy", "amount": 42.0, "unit": "KCAL"}
    ]


async def test_get_food_propagates_upstream_errors(monkeypatch):
    class Client:
        def get_food(self, fdc_id):
            raise TimeoutError("slow")

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    with pytest.raises(TimeoutError):
        await usda_fdc.get_food(123)


# ── check_connectivity (drives /health) ───────────────────────────────

async def test_connectivity_unconfigured_is_distinct_from_error(monkeypatch):
    """A missing key is a config state, not an outage — /health must not
    report the whole service degraded because of it."""
    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: None)
    status = await usda_fdc.check_connectivity()
    assert status["status"] == "unconfigured"
    assert "FDC_API_KEY" in status["detail"]


async def test_connectivity_ok(monkeypatch):
    class Client:
        def search(self, query, page_size=1):
            return _FakeSearchResult()

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    status = await usda_fdc.check_connectivity()
    assert status == {"status": "ok", "total_foods": 1}


async def test_connectivity_error_is_reported_not_raised(monkeypatch):
    """/health must survive an upstream outage."""
    class Client:
        def search(self, query, page_size=1):
            raise ConnectionError("FDC unreachable")

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())
    status = await usda_fdc.check_connectivity()
    assert status["status"] == "error"
    assert "unreachable" in status["detail"]


# ══ Open Food Facts ═══════════════════════════════════════════════════

class _FakeProductApi:
    def __init__(self, payload=None, raises=None, search_payload=None):
        self._payload, self._raises = payload, raises
        self._search_payload = search_payload
        self.calls = []

    def get(self, barcode, fields=None):
        self.calls.append(("get", barcode, fields))
        if self._raises:
            raise self._raises
        return self._payload

    def text_search(self, query, page_size=25):
        self.calls.append(("search", query, page_size))
        if self._raises:
            raise self._raises
        return self._search_payload


class _FakeOffApi:
    def __init__(self, **kw):
        self.product = _FakeProductApi(**kw)


RAW_OFF = {
    "code": "3017620422003",
    "product_name": "Nutella",
    "brands": "Ferrero",
    "image_url": "https://img.example/n.jpg",
    "ingredients_text": "Sugar, palm oil",
    "quantity": "400 g",
    "serving_size": "15 g",
    "categories_tags": ["en:spreads"],
    "allergens_tags": ["en:milk"],
    "labels_tags": ["en:no-gluten"],
    "nutriments": {"energy-kcal_100g": 539.0, "fat_100g": 30.9, "junk": 1},
}


# ── client bootstrap ──────────────────────────────────────────────────

def test_off_client_is_a_singleton(monkeypatch):
    monkeypatch.setattr(off, "_get_off_api", off._get_off_api)  # use the real one
    first = off._get_off_api()
    assert off._get_off_api() is first
    assert first is not None


# ── get_product ───────────────────────────────────────────────────────

async def test_get_product_formats_the_payload(monkeypatch):
    monkeypatch.setattr(off, "_get_off_api", lambda: _FakeOffApi(payload=RAW_OFF))
    p = await off.get_product("3017620422003")

    assert p["barcode"] == "3017620422003"
    assert p["product_name"] == "Nutella"
    assert p["brands"] == "Ferrero"
    assert p["categories"] == ["en:spreads"]
    assert p["allergens"] == ["en:milk"]
    assert p["labels"] == ["en:no-gluten"]
    assert p["nutrients_per_100g"] == {"calories_kcal": 539.0, "fat": 30.9}


async def test_get_product_requests_only_the_fields_we_use(monkeypatch):
    """OFF payloads are huge; we ask for a narrow field set."""
    api = _FakeOffApi(payload=RAW_OFF)
    monkeypatch.setattr(off, "_get_off_api", lambda: api)
    await off.get_product("3017620422003")

    _, barcode, fields = api.product.calls[0]
    assert barcode == "3017620422003"
    assert "nutriments" in fields and "product_name" in fields
    assert "categories_tags" in fields


async def test_get_product_none_when_barcode_unknown(monkeypatch):
    """OFF answers 200 with an empty body for unknown barcodes."""
    monkeypatch.setattr(off, "_get_off_api", lambda: _FakeOffApi(payload={}))
    assert await off.get_product("00000000") is None


async def test_get_product_none_when_product_has_no_name(monkeypatch):
    """A nameless OFF stub record is not a usable product."""
    monkeypatch.setattr(
        off, "_get_off_api", lambda: _FakeOffApi(payload={"code": "1", "nutriments": {}}),
    )
    assert await off.get_product("1") is None


async def test_get_product_none_when_client_unavailable(monkeypatch):
    monkeypatch.setattr(off, "_get_off_api", lambda: None)
    assert await off.get_product("1") is None


async def test_get_product_propagates_upstream_errors(monkeypatch):
    monkeypatch.setattr(
        off, "_get_off_api", lambda: _FakeOffApi(raises=ConnectionError("OFF down")),
    )
    with pytest.raises(ConnectionError):
        await off.get_product("1")


# ── search ────────────────────────────────────────────────────────────

async def test_off_search_formats_every_product(monkeypatch):
    payload = {"count": 2, "products": [RAW_OFF, RAW_OFF]}
    monkeypatch.setattr(
        off, "_get_off_api", lambda: _FakeOffApi(search_payload=payload),
    )
    result = await off.search("nutella")

    assert result["total"] == 2
    assert len(result["products"]) == 2
    assert result["products"][0]["product_name"] == "Nutella"


async def test_off_search_none_when_no_products_key(monkeypatch):
    monkeypatch.setattr(
        off, "_get_off_api", lambda: _FakeOffApi(search_payload={"count": 0}),
    )
    assert await off.search("nothing") is None


async def test_off_search_none_when_client_unavailable(monkeypatch):
    monkeypatch.setattr(off, "_get_off_api", lambda: None)
    assert await off.search("x") is None


async def test_off_search_propagates_upstream_errors(monkeypatch):
    monkeypatch.setattr(
        off, "_get_off_api", lambda: _FakeOffApi(raises=ConnectionError("OFF down")),
    )
    with pytest.raises(ConnectionError):
        await off.search("x")


# ── nutrient extraction ───────────────────────────────────────────────

def test_extract_nutrients_ignores_unknown_keys():
    assert off._extract_nutrients({"fat_100g": 1.0, "nutrition-score-fr": 12}) == {"fat": 1.0}


def test_extract_nutrients_keeps_explicit_zero():
    """0 g of fat is a fact, not a missing value — it must not be dropped."""
    assert off._extract_nutrients({"fat_100g": 0})["fat"] == 0


def test_extract_nutrients_uses_our_field_names():
    """Both upstreams hand the orchestrator the same shape, keyed by the fields
    we publish rather than each vendor's own spelling."""
    result = off._extract_nutrients({"sodium_100g": 0.4, "saturated-fat_100g": 2.5})
    assert result == {"sodium": 0.4, "saturated_fat": 2.5}


def test_extract_nutrients_of_empty_payload():
    assert off._extract_nutrients({}) == {}


def test_format_product_tolerates_missing_fields():
    """OFF records are user-contributed and frequently sparse."""
    p = off._format_product({"code": "1", "product_name": "Bare"})
    assert p["brands"] is None
    assert p["categories"] == []
    assert p["nutrients_per_100g"] == {}


# ── check_connectivity (drives /health) ───────────────────────────────

async def test_off_connectivity_ok(monkeypatch):
    async def found(barcode, *a, **k):
        return {"product_name": "Nutella"}

    monkeypatch.setattr(off, "get_product", found)
    assert await off.check_connectivity() == {"status": "ok"}


async def test_off_connectivity_error_when_probe_product_missing(monkeypatch):
    async def missing(barcode, *a, **k):
        return None

    monkeypatch.setattr(off, "get_product", missing)
    status = await off.check_connectivity()
    assert status["status"] == "error"


async def test_off_connectivity_error_is_reported_not_raised(monkeypatch):
    async def boom(barcode, *a, **k):
        raise ConnectionError("OFF unreachable")

    monkeypatch.setattr(off, "get_product", boom)
    status = await off.check_connectivity()
    assert status["status"] == "error"
    assert "unreachable" in status["detail"]


# ══ Health probes must be bounded ═════════════════════════════════════

async def test_off_health_probe_times_out_rather_than_hanging(monkeypatch):
    """The availability trap: /health is what DigitalOcean and systemd poll.
    An unbounded probe lets a stalled *third party* hang the health endpoint,
    so the platform declares us unhealthy and restarts a container that was
    perfectly able to keep serving degraded responses."""
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.1)

    async def stalls(barcode, *a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(off, "get_product", stalls)

    start = time.monotonic()
    status = await off.check_connectivity()
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"health probe hung for {elapsed:.1f}s"
    assert status["status"] == "error"
    assert "timed out" in status["detail"]


async def test_usda_health_probe_times_out_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.1)

    class StallingClient:
        def search(self, query, page_size=1):
            # Outlives the timeout, but stays short: wait_for cancels the
            # await, not the blocking thread underneath it.
            time.sleep(0.5)

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: StallingClient())

    start = time.monotonic()
    status = await usda_fdc.check_connectivity()
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"health probe hung for {elapsed:.1f}s"
    assert status["status"] == "error"
    assert "timed out" in status["detail"]


# ── FDC key provisioning (for the status page) ────────────────────────

def test_key_status_reports_configured_for_a_real_key(monkeypatch):
    monkeypatch.setenv("FDC_API_KEY", "a-real-looking-key-123")
    assert usda_fdc.key_status() == "configured"


def test_key_status_reports_demo_for_the_shared_demo_key(monkeypatch):
    monkeypatch.setenv("FDC_API_KEY", "DEMO_KEY")
    assert usda_fdc.key_status() == "demo"


def test_key_status_reports_missing_when_unset(monkeypatch):
    monkeypatch.delenv("FDC_API_KEY", raising=False)
    assert usda_fdc.key_status() == "missing"
