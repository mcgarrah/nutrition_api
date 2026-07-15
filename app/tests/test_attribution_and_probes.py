"""
Tests for source attribution and for the cached health probes.

Attribution is a **licence condition**, not decoration. Open Food Facts
publishes its database under the Open Database License (ODbL 1.0) and its
product images under CC BY-SA 3.0; both require attribution, and ODbL adds a
share-alike obligation on derived databases. This service redistributes
OFF-derived names, brands, ingredient text, allergens, labels and image URLs on
every lookup, so a response carrying that data and no attribution is a
compliance failure, not a cosmetic one.

The probe tests cover the other half of the same review: /health used to make a
live Open Food Facts call on *every* poll, while being exempt from the inbound
rate limiter and bypassing the outbound one. That turned the health endpoint
into an amplifier any caller could point at a nonprofit's API — for which Open
Food Facts' stated remedy is an IP ban.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core import attribution, ratelimit, resilience
from app.core import open_food_facts as off
from app.core import orchestrator, usda_fdc
from app.core.resilience import CachedProbe
from app.main import app

client = TestClient(app)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ══ Attribution ═══════════════════════════════════════════════════════

def test_open_food_facts_is_credited_with_its_licence():
    off_terms = attribution.SOURCE_ATTRIBUTION["OpenFoodFacts"]

    assert off_terms["name"] == "Open Food Facts"
    assert "ODbL" in off_terms["license"]
    assert "opendatacommons.org" in off_terms["license_url"]
    assert "CC BY-SA" in off_terms["notes"]        # the image licence
    assert "share-alike" in off_terms["notes"].lower()


def test_every_source_declares_a_licence():
    for source, terms in attribution.SOURCE_ATTRIBUTION.items():
        assert terms["name"], source
        assert terms["license"], source
        assert terms["url"].startswith("https://"), source


def test_attribution_covers_every_source_the_api_can_report():
    """A source that can appear in data_sources but has no attribution entry is
    a source we would be redistributing uncredited."""
    from app.tests.test_public_contract import SOURCE_NAMES

    assert SOURCE_NAMES <= set(attribution.SOURCE_ATTRIBUTION)


def test_only_contributing_sources_are_credited():
    """Crediting Open Food Facts on a response it had no part in would be as
    wrong as omitting it from one it did."""
    credited = attribution.for_sources(["USDA_FDC", "GS1_GPC"])

    assert set(credited) == {"USDA_FDC", "GS1_GPC"}
    assert "OpenFoodFacts" not in credited


def test_unknown_sources_are_ignored():
    assert attribution.for_sources(["NotASource"]) == {}


# ── attribution travels with the data ─────────────────────────────────

def patch_sources(monkeypatch, off_data=None, usda_data=None, gpc=None):
    async def fake_off(barcode, *a, **k):
        return off_data, 1.0, None

    async def fake_usda(upc, *a, **k):
        return usda_data, 1.0, None

    async def fake_gpc(categories):
        return gpc or [], 1.0

    monkeypatch.setattr(orchestrator, "_fetch_off", fake_off)
    monkeypatch.setattr(orchestrator, "_fetch_usda", fake_usda)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", fake_gpc)


async def test_a_lookup_carrying_off_data_carries_its_licence(monkeypatch, off_product):
    """ODbL attribution must accompany the data, not live only in the docs —
    a consumer reading this response has to be told the terms it arrives under."""
    patch_sources(monkeypatch, off_data=off_product)

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["ingredients_text"]                       # OFF-derived data...
    assert "OpenFoodFacts" in body["attribution"]         # ...and its licence
    assert "ODbL" in body["attribution"]["OpenFoodFacts"]["license"]


async def test_a_usda_only_lookup_does_not_credit_off(monkeypatch, usda_food):
    patch_sources(monkeypatch, usda_data=usda_food)

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["data_sources"] == ["USDA_FDC"]
    assert set(body["attribution"]) == {"USDA_FDC"}


async def test_attribution_survives_a_cache_hit(monkeypatch, off_product):
    patch_sources(monkeypatch, off_data=off_product)

    client.get("/api/v1/lookup/028400642255")
    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["cached"] is True
    assert "OpenFoodFacts" in body["attribution"]


# ── the attribution endpoint ──────────────────────────────────────────

def test_attribution_endpoint_lists_every_source():
    body = client.get("/api/v1/attribution").json()

    assert set(body["sources"]) == set(attribution.SOURCE_ATTRIBUTION)
    assert "ODbL" in body["sources"]["OpenFoodFacts"]["license"]


def test_attribution_endpoint_is_not_rate_limited():
    """It costs no upstream call, and a client should never be shed while
    trying to find out how to comply with the licence."""
    for _ in range(50):
        assert client.get("/api/v1/attribution").status_code == 200


def test_the_openapi_description_carries_the_licence_notice():
    spec = client.get("/openapi.json").json()
    description = spec["info"]["description"]

    assert "Open Food Facts" in description
    assert "ODbL" in description


def test_the_ui_carries_the_licence_notice():
    """The tester page renders Open Food Facts images and ingredient text, so
    it owes the same attribution the API does."""
    page = client.get("/").text

    assert "Open Food Facts" in page
    assert "ODbL" in page
    assert "CC BY-SA" in page


# ══ Cached health probes ══════════════════════════════════════════════

@pytest.fixture(autouse=True)
def clear_probes():
    off._probe.clear()
    usda_fdc._probe.clear()
    yield
    off._probe.clear()
    usda_fdc._probe.clear()


def test_cached_probe_expires():
    clock = FakeClock()
    probe = CachedProbe("test", ttl_s=60, timer=clock)

    probe.store({"status": "ok"})
    assert probe.fresh() == {"status": "ok"}

    clock.advance(61)
    assert probe.fresh() is None
    assert probe.last_known() == {"status": "ok"}     # stale, but better than nothing


def test_cached_probe_returns_a_copy():
    probe = CachedProbe("test")
    probe.store({"status": "ok"})

    probe.fresh()["status"] = "MUTATED"
    assert probe.fresh()["status"] == "ok"


async def test_repeated_health_polls_make_one_upstream_call(monkeypatch):
    """The amplification bug: /health is exempt from the inbound limiter, so an
    unbounded probe let any caller drive unlimited traffic at Open Food Facts
    through us. Measured before the fix: 20 polls -> 20 live OFF requests."""
    calls = []

    async def counting(barcode, *a, **k):
        calls.append(barcode)
        return {"product_name": "Nutella"}

    monkeypatch.setattr(off, "get_product", counting)

    for _ in range(20):
        status = await off.check_connectivity()
        assert status["status"] == "ok"

    assert len(calls) == 1


async def test_the_probe_is_charged_to_the_outbound_budget(monkeypatch):
    """Health checks that bypassed the budget could push us past Open Food
    Facts' 15/minute allowance without the limiter ever knowing.

    Patched at the SDK, beneath the gate: the budget is spent inside
    get_product(), so patching get_product would step over the very thing
    under test."""
    class Sdk:
        class product:
            @staticmethod
            def get(barcode, fields=None):
                return {"code": barcode, "product_name": "Nutella", "nutriments": {}}

    monkeypatch.setattr(off, "_get_off_api", lambda: Sdk())
    before = ratelimit.off_limiter.tokens

    await off.check_connectivity()

    assert ratelimit.off_limiter.tokens == pytest.approx(before - 1, abs=0.1)


async def test_an_exhausted_budget_serves_the_last_known_verdict(monkeypatch):
    """Better a stale answer than either lying or spending a token we lack."""
    async def ok(barcode, *a, **k):
        return {"product_name": "Nutella"}

    monkeypatch.setattr(off, "get_product", ok)
    await off.check_connectivity()                    # populate

    off._probe._at -= resilience.HEALTH_PROBE_TTL_S + 1   # force staleness
    monkeypatch.setattr(ratelimit, "off_limiter",
                        ratelimit.TokenBucket(rate=0.0001, per=60.0))

    status = await off.check_connectivity()
    assert status["status"] == "ok"                   # the last known verdict


async def test_no_verdict_and_no_budget_reports_unknown_not_ok(monkeypatch):
    """Never claim an upstream is healthy on the strength of never having asked."""
    monkeypatch.setattr(ratelimit, "off_limiter",
                        ratelimit.TokenBucket(rate=0.0001, per=60.0))

    status = await off.check_connectivity()

    assert status["status"] == "unknown"
    assert status["status"] != "ok"


async def test_a_failed_probe_is_cached_too(monkeypatch):
    """An upstream that is down must not be re-probed on every poll either."""
    calls = []

    async def failing(barcode, *a, **k):
        calls.append(barcode)
        raise ConnectionError("OFF unreachable")

    monkeypatch.setattr(off, "get_product", failing)

    for _ in range(10):
        status = await off.check_connectivity()
        assert status["status"] == "error"

    assert len(calls) == 1


async def test_usda_probe_is_cached_as_well(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = 0

        def search(self, query, page_size=1):
            self.calls += 1

            class R:
                total_hits = 1
            return R()

    fake = Client()
    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: fake)

    for _ in range(10):
        assert (await usda_fdc.check_connectivity())["status"] == "ok"

    assert fake.calls == 1
