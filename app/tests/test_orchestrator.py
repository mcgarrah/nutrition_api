"""
Tests for the DataOrchestrator layered merge logic and lookup cache.

Upstream fetchers are monkeypatched so no network access is required.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from app.core import orchestrator


def _patch_sources(monkeypatch, off_data=None, usda_data=None, gpc_hierarchy=None):
    """Replace the three upstream fetchers with canned responses."""
    calls = {"off": 0, "usda": 0, "gpc": 0}

    async def fake_off(barcode, *a, **k):
        calls["off"] += 1
        return off_data, 10.0, None

    async def fake_usda(barcode, *a, **k):
        calls["usda"] += 1
        return usda_data, 20.0, None

    async def fake_gpc(categories):
        calls["gpc"] += 1
        return gpc_hierarchy or [], 1.0

    monkeypatch.setattr(orchestrator, "_fetch_off", fake_off)
    monkeypatch.setattr(orchestrator, "_fetch_usda", fake_usda)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", fake_gpc)
    return calls


async def test_usda_overrides_off_nutrition(monkeypatch, off_product, usda_food):
    _patch_sources(monkeypatch, off_data=off_product, usda_data=usda_food)

    product = await orchestrator.lookup("04963406021372")

    # USDA is authoritative for name and nutrients
    assert product.product_name == "COCA-COLA CLASSIC"
    assert product.brand == "The Coca-Cola Company"
    assert product.calories_kcal == 42.0
    assert product.carbohydrates.value == 10.6
    assert product.sodium.unit == "mg"
    # OFF still contributes media -- USDA FDC has no images to override it with
    assert product.image_url == "https://images.example.org/coke.jpg"
    assert set(product.data_sources) == {"OpenFoodFacts", "USDA_FDC"}
    assert "USDA_FDC" in product.upstream_latency_ms
    assert "OpenFoodFacts" in product.upstream_latency_ms


async def test_off_nutrition_used_when_usda_missing(monkeypatch, off_product):
    _patch_sources(monkeypatch, off_data=off_product, usda_data=None)

    product = await orchestrator.lookup("04963406021372")

    assert product.product_name == "Coca-Cola Classic"
    assert product.calories_kcal == 44.0
    assert product.protein.value == 0.1
    assert product.data_sources == ["OpenFoodFacts"]


async def test_gpc_hierarchy_wins_over_off_tags(monkeypatch, off_product):
    hierarchy = ["Food/Beverage", "Beverages", "Soft Drinks", "Cola Drinks"]
    _patch_sources(monkeypatch, off_data=off_product, gpc_hierarchy=hierarchy)

    product = await orchestrator.lookup("04963406021372")

    assert product.category_hierarchy == hierarchy
    assert "GS1_GPC" in product.data_sources


async def test_off_tags_fallback_when_gpc_empty(monkeypatch, off_product):
    _patch_sources(monkeypatch, off_data=off_product, gpc_hierarchy=[])

    product = await orchestrator.lookup("04963406021372")

    assert product.category_hierarchy == ["Beverages", "Carbonated Drinks"]
    assert "GS1_GPC" not in product.data_sources


async def test_no_sources_returns_empty_product(monkeypatch):
    _patch_sources(monkeypatch)

    product = await orchestrator.lookup("00000000000000")

    assert product.data_sources == []
    assert product.product_name == "Unknown"
    assert product.calories_kcal is None


async def test_successful_lookup_is_cached(monkeypatch, off_product):
    calls = _patch_sources(monkeypatch, off_data=off_product)

    first = await orchestrator.lookup("04963406021372")
    second = await orchestrator.lookup("04963406021372")

    assert calls["off"] == 1  # second hit served from cache

    # The data is identical, but the response says it came from the cache —
    # otherwise a 1 ms cache hit still claims it spent 500 ms querying USDA.
    assert first.cached is False
    assert second.cached is True
    assert second.model_dump(exclude={"cached"}) == first.model_dump(exclude={"cached"})

    # Cached copies are independent — mutating one must not poison the cache
    second.product_name = "MUTATED"
    third = await orchestrator.lookup("04963406021372")
    assert third.product_name == "Coca-Cola Classic"


async def test_empty_result_is_not_cached(monkeypatch):
    calls = _patch_sources(monkeypatch)

    await orchestrator.lookup("00000000000000")
    await orchestrator.lookup("00000000000000")

    assert calls["off"] == 2  # misses are retried, not cached


# ── sources= scoping (PLAN.md item 10) ───────────────────────────────────

async def test_sources_off_skips_the_usda_fetch_entirely(monkeypatch, off_product):
    calls = _patch_sources(monkeypatch, off_data=off_product, usda_data="should never be seen")

    product = await orchestrator.lookup("04963406021372", sources="off")

    assert calls["usda"] == 0  # never awaited, not just filtered out after
    assert calls["off"] == 1
    assert product.data_sources == ["OpenFoodFacts"]
    assert "USDA_FDC" not in product.upstream_latency_ms
    assert "OpenFoodFacts" in product.upstream_latency_ms


async def test_sources_fdc_skips_the_off_fetch_entirely(monkeypatch, usda_food):
    calls = _patch_sources(monkeypatch, usda_data=usda_food)

    product = await orchestrator.lookup("04963406021372", sources="fdc")

    assert calls["off"] == 0  # never awaited, not just filtered out after
    assert calls["usda"] == 1
    assert product.data_sources == ["USDA_FDC"]
    assert "OpenFoodFacts" not in product.upstream_latency_ms
    assert "USDA_FDC" in product.upstream_latency_ms


async def test_a_scoped_lookup_is_never_cached_nor_reads_the_cache(
        monkeypatch, off_product, usda_food):
    calls = _patch_sources(monkeypatch, off_data=off_product, usda_data=usda_food)

    # Two identical scoped requests must each genuinely re-fetch -- a scoped
    # result must never satisfy a later request for the same GTIN, whether
    # scoped the same way or not.
    await orchestrator.lookup("04963406021372", sources="fdc")
    await orchestrator.lookup("04963406021372", sources="fdc")
    assert calls["usda"] == 2

    # And a scoped lookup must not have populated the shared cache with a
    # partial (FDC-only) product that a later unscoped "both" request could
    # silently receive in place of the real merged one.
    both = await orchestrator.lookup("04963406021372")
    assert set(both.data_sources) == {"OpenFoodFacts", "USDA_FDC"}
    assert calls["off"] == 1  # the "both" call is the first real OFF fetch
