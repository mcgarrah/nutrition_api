"""
Tests for cache honesty, cache-key normalization, and image URL validation.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import orchestrator, usda_fdc
from app.core.orchestrator import _cache_key, _http_url
from app.main import app

client = TestClient(app)


@pytest.fixture
def counting_upstreams(monkeypatch):
    """Serve a product and count how many times upstreams were consulted."""
    calls = []

    async def off_counting(barcode, *a, **k):
        calls.append(barcode)
        return {
            "product_name": "Doritos",
            "image_url": "https://images.example.org/d.jpg",
            "nutrients_per_100g": {"calories_kcal": 536.0},
            "categories": [],
            "allergens": [],
            "labels": [],
        }

    async def usda_none(upc, *a, **k):
        return None

    async def no_gpc(categories):
        return [], 0.0

    monkeypatch.setattr(off, "get_product", off_counting)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", no_gpc)
    return calls


# ── the cache must say that it is the cache ───────────────────────────

async def test_a_fresh_response_is_not_marked_cached(counting_upstreams):
    product = await orchestrator.lookup("028400642255")
    assert product.cached is False


async def test_a_cached_response_says_so(counting_upstreams):
    """Without this flag a cache hit is indistinguishable from a fresh fetch:
    it carries the *original* upstream_latency_ms, so a response served in 1 ms
    still claims it spent 173 ms querying USDA. The telemetry lies."""
    await orchestrator.lookup("028400642255")
    second = await orchestrator.lookup("028400642255")

    assert second.cached is True
    assert len(counting_upstreams) == 1          # upstream consulted once


async def test_cached_response_still_carries_the_original_timings(counting_upstreams):
    """The timings are kept — they describe the fetch that produced the data,
    which is useful — but `cached` is what tells you they aren't this request's."""
    first = await orchestrator.lookup("028400642255")
    second = await orchestrator.lookup("028400642255")

    assert second.upstream_latency_ms == first.upstream_latency_ms
    assert second.cached is True


def test_cached_flag_is_exposed_over_http(counting_upstreams):
    assert client.get("/api/v1/lookup/028400642255").json()["cached"] is False
    assert client.get("/api/v1/lookup/028400642255").json()["cached"] is True


def test_cached_flag_is_documented(counting_upstreams):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["CanonicalProduct"]
    assert "cached" in schema["properties"]


# ── cache key is the normalized barcode ───────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("028400642255", "28400642255"),        # GTIN-12 vs unpadded
    ("028400642255", "0028400642255"),      # GTIN-12 vs GTIN-13
    ("028400642255", "00028400642255"),     # GTIN-12 vs GTIN-14
])
def test_padding_variants_share_a_cache_key(a, b):
    assert _cache_key(a) == _cache_key(b)


def test_unnormalizable_barcode_falls_back_to_itself():
    """Never collapse two genuinely different keys onto each other."""
    assert _cache_key("not-a-barcode") == "not-a-barcode"


async def test_padding_variants_hit_the_cache_instead_of_refetching(counting_upstreams):
    """The same product written three ways is one product — it must not cost
    three round trips to USDA and OFF."""
    await orchestrator.lookup("028400642255")
    await orchestrator.lookup("0028400642255")
    await orchestrator.lookup("00028400642255")

    assert len(counting_upstreams) == 1


async def test_a_cache_hit_echoes_the_barcode_the_caller_used(counting_upstreams):
    """Sharing a key must not mean answering with somebody else's spelling."""
    await orchestrator.lookup("028400642255")
    hit = await orchestrator.lookup("00028400642255")

    assert hit.gtin == "00028400642255"      # as asked, not as first cached
    assert hit.cached is True


async def test_different_products_still_get_different_entries(counting_upstreams):
    await orchestrator.lookup("028400642255")
    await orchestrator.lookup("044000032029")

    assert len(counting_upstreams) == 2


# ── image_url must be an http(s) URL ──────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://images.openfoodfacts.org/p/front.jpg",
    "http://images.example.org/a.png",
    "https://example.org/path?size=400",
])
def test_http_urls_are_accepted(url):
    assert _http_url(url) == url


@pytest.mark.parametrize("url", [
    "javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://example.org/x.jpg",
    "//evil.example.org/x.jpg",     # scheme-relative
    "https://",                     # no host
    "not a url at all",
    "",
    "   ",
    None,
    12345,
    {"not": "a url"},
])
def test_non_http_urls_are_rejected(url):
    """OFF is crowdsourced, so image_url is attacker-influenceable, and we hand
    it to every consumer that renders it."""
    assert _http_url(url) is None


async def test_a_dangerous_image_url_never_reaches_the_client(monkeypatch):
    async def off_hostile(barcode, *a, **k):
        return {
            "product_name": "Hostile",
            "image_url": "javascript:alert(document.cookie)",
            "nutrients_per_100g": {},
            "categories": [],
        }

    async def usda_none(upc, *a, **k):
        return None

    async def no_gpc(categories):
        return [], 0.0

    monkeypatch.setattr(off, "get_product", off_hostile)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", no_gpc)

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["image_url"] is None
    assert body["product_name"] == "Hostile"     # the rest of the product survives


async def test_a_valid_image_url_survives(counting_upstreams):
    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["image_url"] == "https://images.example.org/d.jpg"
