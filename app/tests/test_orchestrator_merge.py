"""
Tests for the DataOrchestrator's reconciliation rules and GPC category mapping.

The merge is the heart of the service, and its rules are the ones a consumer
actually depends on:

  * USDA is authoritative for nutrition; OFF values are provisional
  * OFF owns media, ingredients, allergens, labels
  * GS1 GPC owns taxonomy, with OFF's informal tags as a fallback
  * a missing field must never overwrite a present one

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest

from app.core import orchestrator
from app.core.orchestrator import _fetch_gpc_categories, _nv, _usda_nutrient


def patch_sources(monkeypatch, off_data=None, usda_data=None, gpc=None):
    async def fake_off(barcode):
        return off_data, 10.0

    async def fake_usda(barcode):
        return usda_data, 20.0

    async def fake_gpc(categories):
        return gpc or [], 1.0

    monkeypatch.setattr(orchestrator, "_fetch_off", fake_off)
    monkeypatch.setattr(orchestrator, "_fetch_usda", fake_usda)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", fake_gpc)


# ── nutrient helpers ──────────────────────────────────────────────────

def test_nv_builds_a_nutrient_value():
    nv = _nv(10.5)
    assert (nv.value, nv.unit) == (10.5, "g")
    assert _nv(4.0, unit="mg").unit == "mg"


def test_nv_of_none_is_none():
    assert _nv(None) is None


def test_nv_coerces_int_to_float():
    assert isinstance(_nv(5).value, float)


def test_nv_preserves_explicit_zero():
    """0 g is a value, not an absence."""
    assert _nv(0) is not None
    assert _nv(0).value == 0.0


def test_usda_nutrient_extracts_amount():
    assert _usda_nutrient({"Protein": {"amount": 7.1}}, "Protein") == 7.1


def test_usda_nutrient_missing_name_is_none():
    assert _usda_nutrient({}, "Protein") is None


def test_usda_nutrient_null_amount_is_none():
    """FDC records sometimes carry a nutrient entry with no amount."""
    assert _usda_nutrient({"Protein": {"amount": None}}, "Protein") is None


# ── ranked truth: USDA overrides OFF ──────────────────────────────────

async def test_usda_nutrition_overrides_off(monkeypatch, off_product, usda_food):
    patch_sources(monkeypatch, off_product, usda_food)
    p = await orchestrator.lookup("1")

    assert p.calories_kcal == 42.0            # USDA, not OFF's 44.0
    assert p.carbohydrates.value == 10.6      # USDA, not OFF's 11.0
    assert p.product_name == "COCA-COLA CLASSIC"
    assert p.brand == "The Coca-Cola Company"


async def test_off_keeps_fields_usda_does_not_carry(monkeypatch, off_product, usda_food):
    """USDA has no images — OFF's media must survive the override."""
    patch_sources(monkeypatch, off_product, usda_food)
    p = await orchestrator.lookup("1")

    assert p.image_url == "https://images.example.org/coke.jpg"
    assert p.sugars.value == 10.6             # OFF-only nutrient, USDA has none


async def test_usda_absent_nutrient_does_not_erase_off_value(monkeypatch, off_product):
    """The critical merge rule: absence must not overwrite presence."""
    usda = {
        "description": "COLA",
        "nutrients": [
            {"id": 1008, "name": "Energy", "amount": 42.0, "unit": "KCAL"}
        ],
    }
    patch_sources(monkeypatch, off_product, usda)
    p = await orchestrator.lookup("1")

    assert p.calories_kcal == 42.0            # overridden by USDA
    assert p.protein.value == 0.1             # OFF's value survives
    assert p.fat.value == 0.2


async def test_usda_without_nutrients_leaves_off_nutrition_intact(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product, {"description": "COLA", "nutrients": []})
    p = await orchestrator.lookup("1")

    assert p.calories_kcal == 44.0            # OFF's, untouched
    assert p.product_name == "COLA"           # but the name still came from USDA


async def test_usda_without_description_keeps_off_name(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product, {"nutrients": [], "brand_owner": "Acme"})
    p = await orchestrator.lookup("1")

    assert p.product_name == "Coca-Cola Classic"
    assert p.brand == "Acme"


async def test_usda_falls_back_to_brand_name(monkeypatch, off_product):
    usda = {"description": "COLA", "brand_owner": None, "brand_name": "Coke", "nutrients": []}
    patch_sources(monkeypatch, off_product, usda)
    p = await orchestrator.lookup("1")
    assert p.brand == "Coke"


async def test_sodium_from_usda_is_milligrams(monkeypatch, usda_food):
    """USDA reports sodium in mg while OFF reports grams — the unit must say so."""
    patch_sources(monkeypatch, None, usda_food)
    p = await orchestrator.lookup("1")

    assert p.sodium.value == 4.0
    assert p.sodium.unit == "mg"


async def test_usda_ingredients_used_only_when_off_has_none(monkeypatch, usda_food):
    patch_sources(monkeypatch, None, usda_food)
    p = await orchestrator.lookup("1")
    assert p.ingredients_text.startswith("CARBONATED WATER")


async def test_off_ingredients_win_over_usda(monkeypatch, off_product, usda_food):
    """OFF is the ranked source for label text."""
    patch_sources(monkeypatch, off_product, usda_food)
    p = await orchestrator.lookup("1")
    assert p.ingredients_text == "Carbonated water, high fructose corn syrup"


# ── USDA-only and OFF-only products ───────────────────────────────────

async def test_usda_only_product_has_no_media(monkeypatch, usda_food):
    patch_sources(monkeypatch, None, usda_food)
    p = await orchestrator.lookup("1")

    assert p.data_sources == ["USDA_FDC"]
    assert p.image_url is None
    assert p.allergens == []


async def test_off_only_product_keeps_provisional_nutrition(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product, None)
    p = await orchestrator.lookup("1")

    assert p.data_sources == ["OpenFoodFacts"]
    assert p.calories_kcal == 44.0


async def test_off_product_without_name_keeps_the_default(monkeypatch):
    patch_sources(monkeypatch, {"product_name": "", "nutrients_per_100g": {}}, None)
    p = await orchestrator.lookup("1")
    assert p.product_name == "Unknown"


# ── latency telemetry ─────────────────────────────────────────────────

async def test_latency_reported_for_every_source_even_when_it_returns_nothing(monkeypatch):
    """A source that found nothing still cost time — report it."""
    patch_sources(monkeypatch, None, None)
    p = await orchestrator.lookup("1")

    assert set(p.upstream_latency_ms) == {"OpenFoodFacts", "USDA_FDC", "GS1_GPC"}
    assert p.data_sources == []


# ── GPC category mapping ──────────────────────────────────────────────

async def test_gpc_maps_off_tag_to_full_hierarchy(gpc_db):
    """'en:cola-drinks' → the Cola Drinks brick → its full ancestry."""
    hierarchy, ms = await _fetch_gpc_categories(["en:cola-drinks"])

    assert hierarchy == ["Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks"]
    assert ms >= 0


async def test_gpc_matching_is_case_insensitive_via_like(gpc_db):
    hierarchy, _ = await _fetch_gpc_categories(["en:COLA-DRINKS"])
    assert hierarchy[-1] == "Cola Drinks"


async def test_gpc_uses_first_tag_that_matches(gpc_db):
    """Unmatched tags are skipped, not fatal."""
    hierarchy, _ = await _fetch_gpc_categories(["en:nonexistent-thing", "en:lemonade"])
    assert hierarchy[-1] == "Lemonade"


async def test_gpc_only_considers_the_first_three_tags(gpc_db):
    """OFF products carry long tag lists; we cap the scan."""
    tags = ["en:junk1", "en:junk2", "en:junk3", "en:cola-drinks"]
    hierarchy, _ = await _fetch_gpc_categories(tags)
    assert hierarchy == []


async def test_gpc_empty_when_no_tag_matches(gpc_db):
    hierarchy, _ = await _fetch_gpc_categories(["en:totally-unknown"])
    assert hierarchy == []


async def test_gpc_empty_for_no_tags(gpc_db):
    hierarchy, ms = await _fetch_gpc_categories([])
    assert hierarchy == []
    assert ms >= 0


async def test_gpc_handles_tag_without_language_prefix(gpc_db):
    hierarchy, _ = await _fetch_gpc_categories(["Lemonade"])
    assert hierarchy[-1] == "Lemonade"


async def test_gpc_database_failure_degrades_to_empty(monkeypatch):
    """A broken GPC database must not fail the whole lookup."""
    async def broken_db():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(orchestrator, "get_db", broken_db)
    hierarchy, ms = await _fetch_gpc_categories(["en:cola-drinks"])

    assert hierarchy == []
    assert ms >= 0


# ── taxonomy layering in the full lookup ──────────────────────────────

async def test_gpc_hierarchy_wins_over_off_tags(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product, gpc=["Food/Beverage", "Beverages"])
    p = await orchestrator.lookup("1")

    assert p.category_hierarchy == ["Food/Beverage", "Beverages"]
    assert "GS1_GPC" in p.data_sources


async def test_off_tags_are_the_fallback_when_gpc_finds_nothing(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product, gpc=[])
    p = await orchestrator.lookup("1")

    assert p.category_hierarchy == ["Beverages", "Carbonated Drinks"]
    assert "GS1_GPC" not in p.data_sources   # OFF tags aren't a GS1 classification


async def test_no_categories_at_all(monkeypatch, usda_food):
    patch_sources(monkeypatch, None, usda_food, gpc=[])
    p = await orchestrator.lookup("1")
    assert p.category_hierarchy == []


# ── caching ───────────────────────────────────────────────────────────

async def test_cache_serves_a_defensive_copy(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product)
    first = await orchestrator.lookup("1")
    first.product_name = "MUTATED"
    first.data_sources.append("HACKED")

    second = await orchestrator.lookup("1")
    assert second.product_name == "Coca-Cola Classic"
    assert "HACKED" not in second.data_sources


async def test_empty_results_are_never_cached(monkeypatch):
    """A transient outage must not be memoized as 'this product doesn't exist'."""
    calls = {"n": 0}

    async def counting_off(barcode):
        calls["n"] += 1
        return None, 1.0

    monkeypatch.setattr(orchestrator, "_fetch_off", counting_off)
    monkeypatch.setattr(orchestrator, "_fetch_usda", lambda b: _none())
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", lambda c: _empty())

    await orchestrator.lookup("1")
    await orchestrator.lookup("1")
    assert calls["n"] == 2


async def _none():
    return None, 1.0


async def _empty():
    return [], 1.0


async def test_different_gtins_are_cached_separately(monkeypatch, off_product):
    patch_sources(monkeypatch, off_product)
    a = await orchestrator.lookup("111")
    b = await orchestrator.lookup("222")

    assert a.gtin == "111"
    assert b.gtin == "222"


@pytest.mark.parametrize("gtin", ["12345678", "123456789012", "1234567890123", "12345678901234"])
async def test_all_gtin_lengths_flow_through_the_merge(monkeypatch, off_product, gtin):
    patch_sources(monkeypatch, off_product)
    p = await orchestrator.lookup(gtin)
    assert p.gtin == gtin


# ── remaining upstream failure modes ──────────────────────────────────

async def test_off_timeout_is_absorbed(monkeypatch):
    """The OFF side of the timeout handling (mirror of the USDA case)."""
    from app.core import open_food_facts as off_mod
    from app.core import resilience

    monkeypatch.setattr(resilience, "UPSTREAM_TIMEOUT_S", 0.05)

    async def slow(barcode):
        import asyncio as _a
        await _a.sleep(5)

    monkeypatch.setattr(off_mod, "get_product", slow)
    data, latency_ms = await orchestrator._fetch_off("1")

    assert data is None
    assert latency_ms < 500          # returned at the timeout, not after 5s


async def test_usda_circuit_open_skips_the_call(monkeypatch):
    from app.core import resilience
    from app.core import usda_fdc as usda_mod

    calls = {"n": 0}

    async def should_not_run(upc):
        calls["n"] += 1
        return None

    monkeypatch.setattr(usda_mod, "search_by_upc", should_not_run)
    for _ in range(resilience.usda_breaker.failure_threshold):
        resilience.usda_breaker.record_failure()

    data, _ = await orchestrator._fetch_usda("1")

    assert data is None
    assert calls["n"] == 0


async def test_usda_fiber_and_sugars_override_off(monkeypatch, off_product):
    """The remaining two USDA nutrient mappings."""
    usda = {
        "description": "COLA",
        "nutrients": [
            {"id": 1079, "name": "Fiber, total dietary", "amount": 2.5, "unit": "G"},
            {"id": 2000, "name": "Sugars, total including NLEA", "amount": 9.9, "unit": "G"},
        ],
    }
    patch_sources(monkeypatch, off_product, usda)
    p = await orchestrator.lookup("1")

    assert p.fiber.value == 2.5              # OFF had none
    assert p.sugars.value == 9.9             # overrides OFF's 10.6
