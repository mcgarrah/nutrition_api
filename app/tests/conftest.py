"""
Shared test fixtures.

No test in this suite touches the network: the USDA, Open Food Facts, and GS1
boundaries are always stubbed, and GPC queries run against a small fixture
SQLite database built here rather than the 27 MB production import.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import asyncio
import sqlite3

import pytest

import app.database as database
from app.core import open_food_facts as off
from app.core import orchestrator
from app.core import ratelimit
from app.core import resilience
from app.core import fdc_local
from app.core import store
from app.core import usda_fdc


@pytest.fixture(autouse=True)
def isolated_response_store(tmp_path, monkeypatch):
    """Point the on-disk response store at a fresh directory for every test.

    Without this the suite reads and writes the developer's real corpus: tests
    would pass because a record happened to be cached, and a rate-limit test
    would never spend a token because the store answered first. The suite must
    depend on nothing outside itself — the same lesson CI taught when a test
    quietly borrowed a configured FDC_API_KEY.
    """
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "responses")
    monkeypatch.setattr(store, "STORE_ENABLED", True)


@pytest.fixture(autouse=True)
def isolated_fdc_local(tmp_path, monkeypatch):
    """Keep the tests away from any real local FDC database.

    Once data/fdc.sqlite3 exists on a developer's box, the orchestrator would
    answer barcode lookups from it — and tests that mean to exercise the upstream
    path would quietly stop doing so, passing for the wrong reason. Every test
    starts with no local copy; the ones that want one build their own.
    """
    monkeypatch.setattr(fdc_local, "DB_PATH", tmp_path / "absent.sqlite3")
    monkeypatch.setattr(fdc_local, "ARCHIVE_PATH", tmp_path / "absent.sqlite3.xz")
    monkeypatch.setattr(fdc_local, "_db", None)
    monkeypatch.setattr(fdc_local, "_metadata", {})
    yield
    conn = fdc_local._db
    if conn is not None:
        # aiosqlite runs a non-daemon thread per connection; an orphan hangs exit.
        asyncio.run(conn.close())
    fdc_local._db = None


@pytest.fixture(autouse=True)
def reset_shared_state():
    """Reset the process-wide singletons between tests.

    The lookup cache, the circuit breakers, the rate-limit buckets and the
    cached health probes all persist for the life of the process. Without this a cached product, a
    tripped breaker, or a bucket drained by an earlier test leaks into
    unrelated ones — and the rate limiter in particular would shed the rest of
    the suite, since every test hits the API as the same client.
    """
    orchestrator._lookup_cache.clear()
    for breaker in (resilience.usda_breaker, resilience.off_breaker):
        breaker.record_success()
    _refill_rate_limiters()
    off._probe.clear()
    usda_fdc._probe.clear()
    yield
    orchestrator._lookup_cache.clear()


def _refill_rate_limiters():
    """Hand every rate-limit bucket a full allowance again."""
    for bucket in (ratelimit.off_limiter, ratelimit.usda_limiter):
        bucket._tokens = bucket.capacity
        bucket._updated = bucket._timer()
    ratelimit.inbound_limiter._buckets.clear()


# ── GPC fixture database ──────────────────────────────────────────────
# Mirrors the production schema, including the junction tables that let one
# attribute type belong to many bricks (the schema fix this project exists for).

GPC_SCHEMA = """
CREATE TABLE segments (segment_code TEXT PRIMARY KEY, description TEXT);
CREATE TABLE families (
    family_code TEXT PRIMARY KEY, description TEXT,
    segment_code TEXT NOT NULL REFERENCES segments(segment_code)
);
CREATE TABLE classes (
    class_code TEXT PRIMARY KEY, description TEXT,
    family_code TEXT NOT NULL REFERENCES families(family_code)
);
CREATE TABLE bricks (
    brick_code TEXT PRIMARY KEY, description TEXT,
    class_code TEXT NOT NULL REFERENCES classes(class_code)
);
CREATE TABLE attribute_types (att_type_code TEXT PRIMARY KEY, att_type_text TEXT);
CREATE TABLE attribute_values (att_value_code TEXT PRIMARY KEY, att_value_text TEXT);
CREATE TABLE brick_attribute_types (
    brick_code TEXT NOT NULL, att_type_code TEXT NOT NULL,
    PRIMARY KEY (brick_code, att_type_code)
);
CREATE TABLE attribute_type_values (
    att_type_code TEXT NOT NULL, att_value_code TEXT NOT NULL,
    PRIMARY KEY (att_type_code, att_value_code)
);
CREATE TABLE gpc_metadata (key TEXT PRIMARY KEY, value TEXT);
"""

GPC_ROWS = [
    ("INSERT INTO segments VALUES (?, ?)", [
        ("50000000", "Food/Beverage"),
        ("51000000", "Healthcare"),
    ]),
    ("INSERT INTO families VALUES (?, ?, ?)", [
        ("50200000", "Beverages", "50000000"),
        ("50100000", "Fruits/Vegetables", "50000000"),
    ]),
    ("INSERT INTO classes VALUES (?, ?, ?)", [
        ("50202300", "Carbonated Drinks", "50200000"),
        ("50101800", "Fresh Fruits", "50100000"),
    ]),
    ("INSERT INTO bricks VALUES (?, ?, ?)", [
        ("10000201", "Cola Drinks", "50202300"),
        ("10000202", "Lemonade", "50202300"),
        ("10005900", "Apples", "50101800"),
    ]),
    ("INSERT INTO attribute_types VALUES (?, ?)", [
        ("20000100", "Caffeine Presence"),
    ]),
    ("INSERT INTO attribute_values VALUES (?, ?)", [
        ("30000101", "Caffeinated"),
        ("30000102", "Decaffeinated"),
    ]),
    ("INSERT INTO brick_attribute_types VALUES (?, ?)", [
        ("10000201", "20000100"),
    ]),
    ("INSERT INTO attribute_type_values VALUES (?, ?)", [
        ("20000100", "30000101"),
        ("20000100", "30000102"),
    ]),
    ("INSERT INTO gpc_metadata VALUES (?, ?)", [
        ("gpc_version", "test"),
        ("xml_date", "2026-01-01"),
        ("import_timestamp", "2026-01-01T00:00:00"),
    ]),
]


def build_gpc_db(path):
    """Create the fixture GPC database at `path`."""
    conn = sqlite3.connect(path)
    conn.executescript(GPC_SCHEMA)
    for sql, rows in GPC_ROWS:
        conn.executemany(sql, rows)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def gpc_db(tmp_path, monkeypatch):
    """Point app.database at a fresh fixture GPC database for one test.

    The teardown must *close* the connection, not just drop the reference:
    aiosqlite runs a non-daemon worker thread per connection, so an orphaned
    connection keeps that thread alive and hangs the interpreter at exit.
    """
    db_path = build_gpc_db(tmp_path / "gpc_fixture.sqlite3")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_db", None)
    yield db_path

    conn = database._db
    if conn is not None:
        asyncio.run(conn.close())
    database._db = None


# ── Upstream payload fixtures ─────────────────────────────────────────

@pytest.fixture
def off_product():
    """A representative formatted Open Food Facts product."""
    return {
        "barcode": "04963406021372",
        "product_name": "Coca-Cola Classic",
        "brands": "Coca-Cola",
        "image_url": "https://images.example.org/coke.jpg",
        "ingredients_text": "Carbonated water, high fructose corn syrup",
        "categories": ["en:beverages", "en:carbonated-drinks"],
        "allergens": [],
        "labels": [],
        # Keyed by our field names — open_food_facts._extract_nutrients maps
        # OFF's per-100g keys onto them, so both upstreams hand the
        # orchestrator the same shape.
        "nutrients_per_100g": {
            "calories_kcal": 44.0,
            "protein": 0.1,
            "fat": 0.2,
            "carbohydrates": 11.0,
            "sugars": 10.6,
        },
    }


def usda_nutrient(fdc_id: int, name: str, amount, unit: str) -> dict:
    """One entry as app/core/usda_fdc.get_food() emits it."""
    return {"id": fdc_id, "name": name, "amount": amount, "unit": unit}


@pytest.fixture
def usda_food():
    """A representative formatted USDA FDC branded food.

    `nutrients` is a *list* carrying ids, matching what FDC actually returns —
    it publishes energy twice under the identical name "Energy" (kcal id 1008,
    kJ id 1062), so a dict keyed by name silently keeps whichever came last.
    """
    return {
        "fdc_id": 123456,
        "description": "COCA-COLA CLASSIC",
        "brand_owner": "The Coca-Cola Company",
        "brand_name": None,
        "ingredients": "CARBONATED WATER, HIGH FRUCTOSE CORN SYRUP, CARAMEL COLOR",
        "nutrients": [
            usda_nutrient(1008, "Energy", 42.0, "KCAL"),
            usda_nutrient(1003, "Protein", 0.0, "G"),
            usda_nutrient(1004, "Total lipid (fat)", 0.0, "G"),
            usda_nutrient(1005, "Carbohydrate, by difference", 10.6, "G"),
            usda_nutrient(1093, "Sodium, Na", 4.0, "MG"),
        ],
    }
