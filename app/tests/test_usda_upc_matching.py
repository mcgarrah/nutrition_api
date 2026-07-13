"""
Tests for USDA FDC barcode matching.

FDC has no barcode-lookup endpoint — search_by_upc queries a full-text search
that returns fuzzy matches, so an unknown barcode can surface an unrelated
product. These tests pin the guarantee that we only return a food whose own
gtinUpc actually matches the requested barcode.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from usda_fdc.models import SearchResult, SearchResultFood

from app.core import usda_fdc
from app.core.usda_fdc import normalize_gtin


class _FakeClient:
    """Stands in for FdcClient.

    Deliberately builds the library's *real* SearchResultFood objects via
    from_api_data, so these tests exercise the actual FDC payload parsing. If
    usda_fdc ever drops gtin_upc from its models again — the gap this repo used
    to work around by reading the raw payload via a private method — they fail
    here rather than silently serving the wrong product in production.
    """

    def __init__(self, foods):
        self._foods = [SearchResultFood.from_api_data(f) for f in foods]
        self.searches = []

    def search(self, query, data_type=None, page_size=50, **kwargs):
        self.searches.append({
            "query": query, "data_type": data_type, "page_size": page_size,
        })
        return SearchResult(
            foods=self._foods,
            total_hits=len(self._foods),
            current_page=1,
            total_pages=1,
        )


@pytest.fixture
def fake_fdc(monkeypatch):
    """Install a fake FDC client and stub get_food to echo the fdcId."""
    def install(foods):
        client = _FakeClient(foods)
        monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: client)

        async def fake_get_food(fdc_id):
            return {"fdc_id": fdc_id, "description": f"food-{fdc_id}"}

        monkeypatch.setattr(usda_fdc, "get_food", fake_get_food)
        return client

    return install


# ── normalize_gtin ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("028400642255", "00028400642255"),    # GTIN-12 (UPC-A)
    ("0099447210127", "00099447210127"),   # GTIN-13, already zero-padded
    ("12345678", "00000012345678"),        # GTIN-8
    ("00028400642255", "00028400642255"),  # already GTIN-14
    (" 028400642255 ", "00028400642255"),  # stray whitespace
])
def test_normalize_gtin_pads_to_14(raw, expected):
    assert normalize_gtin(raw) == expected


def test_normalize_gtin_rejects_junk_and_overlong():
    assert normalize_gtin("abc") == ""
    assert normalize_gtin("") == ""
    assert normalize_gtin("123456789012345") == ""  # 15 digits — not a GTIN


def test_padding_variants_of_same_gtin_compare_equal():
    """The same barcode at different paddings must not read as different."""
    assert normalize_gtin("099447210127") == normalize_gtin("0099447210127")


# ── search_by_upc ─────────────────────────────────────────────────────

async def test_returns_food_whose_gtin_matches(fake_fdc):
    fake_fdc([
        {"fdcId": 111, "gtinUpc": "028400642255", "description": "Doritos"},
    ])
    result = await usda_fdc.search_by_upc("028400642255")
    assert result["fdc_id"] == 111


async def test_rejects_fuzzy_match_with_unrelated_barcode(fake_fdc):
    """The actual bug: querying 00000000 returned chicken nuggets."""
    fake_fdc([
        {"fdcId": 222, "gtinUpc": "0099447210127", "description": "CHICKEN NUGGETS"},
        {"fdcId": 333, "gtinUpc": "0838927500101", "description": "BASMATI RICE"},
    ])
    assert await usda_fdc.search_by_upc("00000000") is None


async def test_scans_past_non_matching_hits_to_find_the_real_one(fake_fdc):
    """A genuine match must be found even when it isn't the top hit."""
    fake_fdc([
        {"fdcId": 222, "gtinUpc": "0099447210127", "description": "CHICKEN NUGGETS"},
        {"fdcId": 444, "gtinUpc": "028400642255", "description": "Doritos"},
    ])
    result = await usda_fdc.search_by_upc("028400642255")
    assert result["fdc_id"] == 444


async def test_matches_across_zero_padding_difference(fake_fdc):
    """A 12-digit query must match FDC's 13-digit zero-padded record."""
    fake_fdc([
        {"fdcId": 555, "gtinUpc": "0099447210127", "description": "CHICKEN NUGGETS"},
    ])
    result = await usda_fdc.search_by_upc("099447210127")
    assert result["fdc_id"] == 555


async def test_empty_results_return_none(fake_fdc):
    fake_fdc([])
    assert await usda_fdc.search_by_upc("028400642255") is None


async def test_food_missing_gtin_is_not_matched(fake_fdc):
    """Foods with no gtinUpc (some FDC records) must never match."""
    fake_fdc([
        {"fdcId": 666, "description": "no barcode on this record"},
    ])
    assert await usda_fdc.search_by_upc("028400642255") is None


async def test_malformed_barcode_short_circuits_without_calling_upstream(fake_fdc):
    client = fake_fdc([{"fdcId": 777, "gtinUpc": "028400642255"}])
    assert await usda_fdc.search_by_upc("not-a-barcode") is None
    assert client.searches == []  # never hit the API


async def test_queries_branded_foods_via_the_public_search_api(fake_fdc):
    """Uses the library's supported search(), not a private request method.

    The barcode check used to require reading the raw payload because the
    usda_fdc models dropped gtinUpc. usda-fdc 0.1.10 exposes gtin_upc, so this
    repo goes through the public API.
    """
    client = fake_fdc([{"fdcId": 888, "gtinUpc": "028400642255"}])

    await usda_fdc.search_by_upc("028400642255")

    call = client.searches[0]
    assert call["query"] == "028400642255"
    assert call["data_type"] == ["Branded"]


def test_the_library_still_exposes_gtin_upc():
    """A canary on the upstream dependency.

    If usda_fdc ever drops gtin_upc from its search model again, barcode
    verification silently breaks and FDC's fuzzy search starts returning
    unrelated products under the requested barcode. Fail here instead.
    """
    food = SearchResultFood.from_api_data({
        "fdcId": 1, "description": "X", "dataType": "Branded",
        "gtinUpc": "028400642255",
    })
    assert food.gtin_upc == "028400642255"
