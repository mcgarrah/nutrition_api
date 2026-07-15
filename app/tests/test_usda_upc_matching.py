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

        async def fake_get_food(fdc_id, *a, **k):
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


# ── usda-fdc 0.2.0: telling "absent" and "throttled" apart from "broken" ──

def test_the_library_does_not_leak_the_api_key_in_errors():
    """A security canary.

    Before 0.1.11 the key travelled in the query string, and requests embeds
    the full URL in its exception text — so the first network hiccup wrote the
    real key into our logs, which log those exceptions verbatim.
    """
    from usda_fdc import FdcClient

    client = FdcClient(
        api_key="SECRET_KEY_12345",
        base_url="https://127.0.0.1:9/fdc/v1/",
        timeout=1,
    )

    with pytest.raises(Exception) as exc:
        client.search("cola")

    assert "SECRET_KEY_12345" not in str(exc.value)


def test_the_library_still_distinguishes_its_failure_modes():
    """Canary on 0.2.0: if these collapse back into a bare FdcApiError, a
    missing food becomes indistinguishable from a broken API and starts
    tripping the circuit breaker again."""
    from usda_fdc.exceptions import (
        FdcApiError, FdcRateLimitError, FdcResourceNotFoundError,
    )

    assert issubclass(FdcResourceNotFoundError, FdcApiError)
    assert issubclass(FdcRateLimitError, FdcApiError)
    assert FdcResourceNotFoundError is not FdcRateLimitError


async def test_a_missing_food_is_not_an_upstream_failure(monkeypatch):
    """FDC answering "no such food" is a healthy API doing its job. Counting it
    as a failure meant five lookups of missing foods in a row would open the
    circuit and shut USDA out for everyone."""
    from usda_fdc.exceptions import FdcResourceNotFoundError

    from app.core import resilience

    class Client:
        def get_food(self, fdc_id):
            raise FdcResourceNotFoundError("not found")

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())

    for _ in range(resilience.usda_breaker.failure_threshold + 2):
        assert await usda_fdc.get_food(999999) is None

    assert not resilience.usda_breaker.is_open
    assert resilience.usda_breaker._consecutive_failures == 0


async def test_fdc_rate_limiting_us_does_not_trip_the_breaker(monkeypatch):
    """Their 429 is a budgeting fact, not an outage. Opening the circuit on it
    would keep USDA shut out long after the limit had reset."""
    from usda_fdc.exceptions import FdcRateLimitError

    from app.core import resilience

    async def throttled():
        raise FdcRateLimitError("rate limit exceeded")

    for _ in range(resilience.usda_breaker.failure_threshold + 2):
        with pytest.raises(FdcRateLimitError):
            await resilience.usda_breaker.call(throttled)

    assert not resilience.usda_breaker.is_open


async def test_a_real_outage_still_trips_the_breaker(monkeypatch):
    """The exemptions must not swallow genuine failures."""
    from app.core import resilience

    async def down():
        raise ConnectionError("FDC unreachable")

    for _ in range(resilience.usda_breaker.failure_threshold):
        with pytest.raises(ConnectionError):
            await resilience.usda_breaker.call(down)

    assert resilience.usda_breaker.is_open
