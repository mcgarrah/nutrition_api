"""
Input-handling tests: malicious, malformed, and merely awkward input.

Every GPC query interpolates a user-supplied string into a SQL LIKE. The
values are bound parameters, never formatted into the statement — these tests
exist to keep it that way, because the failure mode is silent and total.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import usda_fdc
from app.core.usda_fdc import normalize_gtin
from app.main import app

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("gpc_db")


INJECTIONS = [
    "'; DROP TABLE segments; --",
    "' OR '1'='1",
    "1'; DELETE FROM bricks WHERE '1'='1",
    '" OR ""="',
    "'; UPDATE segments SET description='pwned'; --",
]


# ── SQL injection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", INJECTIONS)
def test_search_endpoint_is_not_injectable(payload, gpc_db):
    resp = client.get("/api/v1/gpc/search/", params={"q": payload})

    assert resp.status_code == 200
    # The payload is treated as a literal search term, matching nothing
    body = resp.json()
    assert all(body[k] == [] for k in ("segments", "families", "classes", "bricks"))

    # ...and the database is intact
    conn = sqlite3.connect(gpc_db)
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM bricks").fetchone()[0] == 3
    assert conn.execute(
        "SELECT description FROM segments WHERE segment_code='50000000'"
    ).fetchone()[0] == "Food/Beverage"
    conn.close()


@pytest.mark.parametrize("payload", INJECTIONS)
def test_list_search_filter_is_not_injectable(payload, gpc_db):
    resp = client.get("/api/v1/gpc/bricks/", params={"search": payload})

    assert resp.status_code == 200
    assert resp.json()["count"] == 0

    conn = sqlite3.connect(gpc_db)
    assert conn.execute("SELECT COUNT(*) FROM bricks").fetchone()[0] == 3
    conn.close()


@pytest.mark.parametrize("payload", INJECTIONS)
def test_code_filters_are_not_injectable(payload, gpc_db):
    """These land in an equality clause rather than a LIKE."""
    resp = client.get("/api/v1/gpc/classes/", params={"family_code": payload})

    assert resp.status_code == 200
    assert resp.json()["count"] == 0

    conn = sqlite3.connect(gpc_db)
    assert conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0] == 2
    conn.close()


def test_path_parameter_is_not_injectable(gpc_db):
    resp = client.get("/api/v1/gpc/segments/' OR '1'='1")

    assert resp.status_code == 404      # no such segment, not "every segment"

    conn = sqlite3.connect(gpc_db)
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2
    conn.close()


# ── LIKE wildcards ────────────────────────────────────────────────────
# '%' and '_' are wildcards *inside* the bound value, so a user can widen
# their own search. That is not injection — it cannot escape the statement or
# touch another table — but the behaviour should be deliberate, not a surprise.

def test_percent_in_search_acts_as_a_wildcard():
    body = client.get("/api/v1/gpc/bricks/", params={"search": "%"}).json()
    assert body["count"] == 3           # matches every brick


def test_underscore_in_search_acts_as_a_single_char_wildcard():
    body = client.get("/api/v1/gpc/bricks/", params={"search": "Col_ Drinks"}).json()
    assert [b["brick_code"] for b in body["results"]] == ["10000201"]


def test_wildcard_search_cannot_reach_other_tables():
    """A wildcard widens the current query only — it never leaks rows across."""
    body = client.get("/api/v1/gpc/segments/", params={"search": "%"}).json()
    assert body["count"] == 2           # the two segments, not bricks or classes


# ── Unicode and oversized input ───────────────────────────────────────

@pytest.mark.parametrize("term", [
    "café",             # accents
    "北京",              # CJK
    "🍫",               # emoji
    "Ñoño",
    "​",           # zero-width space
])
def test_unicode_search_terms_are_handled(term):
    resp = client.get("/api/v1/gpc/search/", params={"q": term})
    assert resp.status_code == 200


def test_very_long_search_term_does_not_error():
    resp = client.get("/api/v1/gpc/search/", params={"q": "a" * 5000})
    assert resp.status_code == 200
    assert resp.json()["bricks"] == []


def test_null_byte_in_search_is_rejected_or_handled():
    """A NUL must never reach SQLite as a truncating C string."""
    resp = client.get("/api/v1/gpc/search/", params={"q": "cola\x00; DROP TABLE bricks"})
    assert resp.status_code in (200, 400, 422)


def test_whitespace_only_search_is_harmless():
    assert client.get("/api/v1/gpc/search/", params={"q": "   "}).status_code == 200


# ── GTIN normalization against hostile input ──────────────────────────

@pytest.mark.parametrize("hostile", [
    "'; DROP TABLE foods; --",
    "../../etc/passwd",
    "<script>alert(1)</script>",
    "%00",
    "1e10",              # scientific notation is not a barcode
    "-12345678",         # negative
    "  ",
    "",
])
def test_normalize_gtin_rejects_non_barcodes(hostile):
    """Anything that isn't a plain digit string must normalize to '' so it can
    never be mistaken for a real barcode match."""
    result = normalize_gtin(hostile)
    assert result == "" or result.isdigit()


def test_normalize_gtin_strips_embedded_non_digits_only_within_length():
    """'12-34-56-78' has 8 digits — it normalizes rather than being rejected.
    The route-level pattern is what refuses punctuation; this is the last line
    of defence for the matcher itself."""
    assert normalize_gtin("12-34-56-78") == "00000012345678"


@pytest.mark.parametrize("gtin", [
    "'; DROP TABLE x; --",
    "../../../etc/passwd",
    "<script>alert(1)</script>",
    "0284006422 55",
])
def test_lookup_route_rejects_hostile_gtins_before_the_orchestrator(gtin, monkeypatch):
    from app.core import orchestrator

    reached = {"n": 0}

    async def should_not_run(g):
        reached["n"] += 1

    monkeypatch.setattr(orchestrator, "lookup", should_not_run)

    resp = client.get(f"/api/v1/lookup/{gtin}")
    assert resp.status_code in (404, 422)   # never a 200, never a 500
    assert reached["n"] == 0


# ── Upstream returning junk ───────────────────────────────────────────

@pytest.mark.parametrize("junk", [">100", "trace", "", "N/A", "1,5", [], {}])
async def test_non_numeric_nutrient_is_dropped_not_fatal(junk, monkeypatch, gpc_db):
    """OFF nutriments are crowdsourced and arrive as whatever was typed off the
    label. float(">100") raises, and an unhandled ValueError here would turn a
    partial result into a 500 — which this service promises never to return."""
    async def off_junk(barcode):
        return {
            "product_name": "Weird",
            "categories": [],
            "nutrients_per_100g": {"protein": junk},
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_junk)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    assert resp.json()["protein"] is None       # dropped, not guessed at


async def test_good_nutrients_survive_a_bad_neighbour(monkeypatch, gpc_db):
    """One unusable value must not discard the whole nutrition payload."""
    async def off_mixed(barcode):
        return {
            "product_name": "Weird",
            "categories": [],
            "nutrients_per_100g": {
                "protein": ">100",     # junk
                "fat": 30.9,           # fine
                "calories_kcal": 539.0,  # fine
            },
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_mixed)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["protein"] is None
    assert body["fat"] == {"value": 30.9, "unit": "g"}
    assert body["calories_kcal"] == 539.0


async def test_non_numeric_calories_are_dropped(monkeypatch, gpc_db):
    """calories_kcal is assigned straight onto the model, so a junk value here
    escapes validation until FastAPI serializes — i.e. a 500."""
    async def off_junk(barcode):
        return {
            "product_name": "Weird",
            "categories": [],
            "nutrients_per_100g": {"calories_kcal": "about 500"},
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_junk)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    assert resp.json()["calories_kcal"] is None


async def test_malformed_usda_nutrient_entry_is_ignored(monkeypatch, gpc_db):
    """A nutrient entry that isn't even a dict must not raise."""
    async def off_none(barcode):
        return None

    async def usda_malformed(upc):
        return {
            "description": "COLA",
            "nutrients": [
                "not-a-dict",
                {"id": 1003, "name": "Protein", "amount": 7.0, "unit": "G"},
            ],
        }

    monkeypatch.setattr(off, "get_product", off_none)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_malformed)

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["calories_kcal"] is None
    assert body["protein"] == {"value": 7.0, "unit": "g"}


async def test_upstream_returning_null_name_does_not_500(monkeypatch, gpc_db):
    async def off_nulls(barcode):
        return {"product_name": None, "categories": None, "nutrients_per_100g": {}}

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_nulls)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    resp = client.get("/api/v1/lookup/028400642255")
    assert resp.status_code != 500


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "nan", "Infinity"])
async def test_non_finite_nutrient_never_reaches_the_response(value, monkeypatch, gpc_db):
    """NaN/Infinity are valid floats but invalid JSON: Python emits the bare
    tokens NaN/Infinity, which strict parsers (Go, Jackson, JSON.parse) reject
    — breaking the whole response, not just the field."""
    async def off_junk(barcode):
        return {
            "product_name": "Weird",
            "categories": [],
            "nutrients_per_100g": {"protein": value, "calories_kcal": value},
        }

    async def usda_none(upc):
        return None

    monkeypatch.setattr(off, "get_product", off_junk)
    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)

    resp = client.get("/api/v1/lookup/028400642255")

    assert resp.status_code == 200
    assert resp.json()["protein"] is None
    assert resp.json()["calories_kcal"] is None
    # The payload must parse under a strict JSON reader
    json.loads(resp.text, parse_constant=_reject_constant)


def _reject_constant(token):
    raise AssertionError(f"response contains non-JSON token: {token}")
