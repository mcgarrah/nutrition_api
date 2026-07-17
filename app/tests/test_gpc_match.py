"""
Tests for the curated FDC-category -> GPC-brick mapping.

The table itself is verified against the real GPC/FDC data separately (that is
what "curated" means — see ARCH.md, "GPC Category Matching"); these tests cover
the lookup and hierarchy-resolution machinery around it, plus a structural
sanity check on the table so a future edit can't silently introduce a
malformed entry.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import re

from app.core.gpc_match import (
    FDC_CATEGORY_TO_BRICK,
    FDC_CATEGORY_TO_CLASS,
    curated_brick_for_fdc_category,
    curated_class_for_fdc_category,
    curated_hierarchy_for_fdc_category,
    hierarchy_for_brick,
    hierarchy_for_class,
)

_BRICK_CODE = re.compile(r"^\d{8}$")
_CLASS_CODE = re.compile(r"^\d{8}$")


# ── Table integrity ─────────────────────────────────────────────────

def test_the_table_is_non_trivial():
    """A curated table with a handful of entries isn't worth having."""
    assert len(FDC_CATEGORY_TO_BRICK) >= 40


def test_every_brick_code_has_the_right_shape():
    """GPC brick codes are 8-digit strings. Catches a fat-fingered entry
    before it reaches the database — a malformed code fails a lookup
    silently (no rows), which is exactly the kind of error curation exists
    to prevent."""
    for category, code in FDC_CATEGORY_TO_BRICK.items():
        assert _BRICK_CODE.match(code), f"{category!r} -> {code!r} is not an 8-digit brick code"


def test_no_category_key_is_blank_or_whitespace_only():
    for category in FDC_CATEGORY_TO_BRICK:
        assert category.strip(), "a blank category key can never match anything"


def test_no_dict_key_has_unstripped_surrounding_whitespace():
    """curated_brick_for_fdc_category / curated_class_for_fdc_category strip
    their input before the lookup (see test_surrounding_whitespace_is_stripped
    below) -- so a dict key with a literal leading/trailing space (FDC has at
    least one real category like this: "Cheese - Speciality ") can NEVER be
    reached, since the stripped input will never equal the unstripped key.
    This silently shipped once already; this test catches it structurally so
    it can't happen again without a test failure pointing at the exact key."""
    for category in list(FDC_CATEGORY_TO_BRICK) + list(FDC_CATEGORY_TO_CLASS):
        assert category == category.strip(), (
            f"{category!r} has surrounding whitespace and can never match a "
            f"lookup, which always strips its input first -- store it stripped"
        )


# ── curated_brick_for_fdc_category ────────────────────────────────────

def test_a_known_category_resolves():
    assert curated_brick_for_fdc_category("Cheese") == "10000028"


def test_an_unknown_category_returns_none():
    assert curated_brick_for_fdc_category("Artisanal Yak Jerky") is None


def test_none_input_returns_none():
    assert curated_brick_for_fdc_category(None) is None


def test_empty_string_returns_none():
    assert curated_brick_for_fdc_category("") is None


def test_surrounding_whitespace_is_stripped():
    """FDC's own category field is not always clean (see the Baby/Infant
    double-space case); a stray space either side must not break a match."""
    assert curated_brick_for_fdc_category("  Cheese  ") == "10000028"


def test_matching_is_exact_not_fuzzy():
    """The whole point of a curated table is that it does NOT guess. A near
    miss must return None, not the nearest plausible entry."""
    assert curated_brick_for_fdc_category("Cheeses") is None
    assert curated_brick_for_fdc_category("cheese") is None  # case-sensitive


def test_the_two_baby_infant_spellings_both_resolve():
    """FDC's own data has two spellings of this one category; both are
    curated so neither silently falls through to the weaker fuzzy path."""
    assert curated_brick_for_fdc_category("Baby/Infant  Foods/Beverages") == "10000610"
    assert curated_brick_for_fdc_category("Baby/Infant - Foods/Beverages") == "10000610"


# ── hierarchy_for_brick ────────────────────────────────────────────────

async def test_hierarchy_for_a_known_brick(gpc_db):
    from app.database import get_db
    db = await get_db()
    hierarchy = await hierarchy_for_brick(db, "10000201")  # Cola Drinks, in the fixture
    assert hierarchy == ["Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks"]


async def test_hierarchy_for_an_unknown_brick_is_empty(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await hierarchy_for_brick(db, "99999999") == []


# ── Class table integrity ───────────────────────────────────────────────

def test_the_class_table_is_non_trivial():
    assert len(FDC_CATEGORY_TO_CLASS) >= 60


def test_every_class_code_has_the_right_shape():
    for category, code in FDC_CATEGORY_TO_CLASS.items():
        assert _CLASS_CODE.match(code), f"{category!r} -> {code!r} is not an 8-digit class code"


def test_no_class_category_key_is_blank_or_whitespace_only():
    for category in FDC_CATEGORY_TO_CLASS:
        assert category.strip(), "a blank category key can never match anything"


def test_brick_and_class_tables_do_not_claim_the_same_category():
    """A category curated at brick level should not also sit in the class
    table -- the brick-level entry is strictly more specific and the class
    entry would be dead weight (curated_hierarchy_for_fdc_category always
    prefers the brick, so a class duplicate could never be reached)."""
    overlap = set(FDC_CATEGORY_TO_BRICK) & set(FDC_CATEGORY_TO_CLASS)
    assert not overlap, f"categories curated at both levels: {overlap}"


# ── curated_class_for_fdc_category ──────────────────────────────────────

def test_a_known_class_category_resolves():
    assert curated_class_for_fdc_category("Bread") == "50181900"


def test_an_unknown_class_category_returns_none():
    assert curated_class_for_fdc_category("Artisanal Yak Jerky") is None


def test_class_lookup_of_none_returns_none():
    assert curated_class_for_fdc_category(None) is None


def test_class_lookup_whitespace_is_stripped():
    assert curated_class_for_fdc_category("  Bread  ") == "50181900"


# ── hierarchy_for_class ──────────────────────────────────────────────────

async def test_hierarchy_for_a_known_class(gpc_db):
    from app.database import get_db
    db = await get_db()
    hierarchy = await hierarchy_for_class(db, "50101800")  # Fresh Fruits, in the fixture
    assert hierarchy == ["Food/Beverage", "Fruits/Vegetables", "Fresh Fruits"]


async def test_hierarchy_for_an_unknown_class_is_empty(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await hierarchy_for_class(db, "99999999") == []


# ── curated_hierarchy_for_fdc_category (combined resolver) ──────────────

async def test_combined_resolver_prefers_brick_over_class(gpc_db, monkeypatch):
    from app.core import gpc_match
    from app.database import get_db
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {"Fizzy": "10000201"})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {"Fizzy": "50101800"})
    db = await get_db()
    hierarchy = await curated_hierarchy_for_fdc_category(db, "Fizzy")
    assert hierarchy == ["Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks"]


async def test_combined_resolver_falls_back_to_class_when_no_brick(gpc_db, monkeypatch):
    from app.core import gpc_match
    from app.database import get_db
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_BRICK", {})
    monkeypatch.setattr(gpc_match, "FDC_CATEGORY_TO_CLASS", {"Fruit": "50101800"})
    db = await get_db()
    hierarchy = await curated_hierarchy_for_fdc_category(db, "Fruit")
    assert hierarchy == ["Food/Beverage", "Fruits/Vegetables", "Fresh Fruits"]


async def test_combined_resolver_returns_empty_for_no_match(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await curated_hierarchy_for_fdc_category(db, "Artisanal Yak Jerky") == []


async def test_combined_resolver_of_none_returns_empty(gpc_db):
    from app.database import get_db
    db = await get_db()
    assert await curated_hierarchy_for_fdc_category(db, None) == []
