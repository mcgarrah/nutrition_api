"""
Tests for the on-disk response store and its SQLite importer.

The store's first job is not archival — it is staying inside the upstream
budgets. The in-memory cache dies with the process, so before this every deploy
re-spent Open Food Facts' entire allowance (fifteen requests a minute) on
barcodes we had already fetched. A stored response costs no request at all,
which is checked here by asserting the rate-limit budget is untouched on a hit.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import open_food_facts as off
from app.core import ratelimit, store, usda_fdc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_store_to_sqlite as importer  # noqa: E402


# ══ The record format ═════════════════════════════════════════════════

def test_a_record_round_trips():
    store.put(store.OFF_PRODUCT, "3017620422003", {"product_name": "Nutella"})

    assert store.get(store.OFF_PRODUCT, "3017620422003") == {"product_name": "Nutella"}


def test_a_missing_record_is_none():
    assert store.get(store.OFF_PRODUCT, "00000000") is None


def test_timestamps_are_utc_with_an_explicit_offset():
    """A naive timestamp in an archive is worse than none: it is wrong
    somewhere, and it does not say where."""
    store.put(store.OFF_PRODUCT, "1", {"a": 1})

    record = json.loads(store.path_for(store.OFF_PRODUCT, "1").read_text())
    fetched_at = datetime.fromisoformat(record["fetched_at"])

    assert fetched_at.tzinfo is not None
    assert fetched_at.utcoffset() == timedelta(0)          # UTC, not merely aware
    assert "+00:00" in record["fetched_at"]


def test_a_record_is_self_describing():
    """The importer reads these files years from now, with no application
    around to explain them."""
    store.put(store.USDA_FOOD, "12345", {"description": "COLA"})

    record = json.loads(store.path_for(store.USDA_FOOD, "12345").read_text())

    assert record["namespace"] == store.USDA_FOOD
    assert record["key"] == "12345"
    assert record["schema_version"] == store.SCHEMA_VERSION
    assert record["payload"] == {"description": "COLA"}


def test_records_are_stored_as_readable_json():
    """The corpus should be greppable and diffable, not an opaque blob."""
    store.put(store.OFF_PRODUCT, "1", {"product_name": "Nutella"})

    text = store.path_for(store.OFF_PRODUCT, "1").read_text()

    assert "\n" in text                    # pretty-printed
    assert "Nutella" in text
    json.loads(text)


def test_unicode_survives_the_round_trip():
    store.put(store.OFF_PRODUCT, "1", {"product_name": "Café Noisettes 北京 🍫"})

    assert store.get(store.OFF_PRODUCT, "1")["product_name"] == "Café Noisettes 北京 🍫"


# ══ Freshness ═════════════════════════════════════════════════════════

def test_a_stale_record_is_not_served():
    store.put(store.OFF_PRODUCT, "1", {"a": 1})

    path = store.path_for(store.OFF_PRODUCT, "1")
    record = json.loads(path.read_text())
    record["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(days=store.STORE_TTL_DAYS + 1)
    ).isoformat()
    path.write_text(json.dumps(record))

    assert store.get(store.OFF_PRODUCT, "1") is None


def test_a_record_within_the_ttl_is_served():
    store.put(store.OFF_PRODUCT, "1", {"a": 1})

    path = store.path_for(store.OFF_PRODUCT, "1")
    record = json.loads(path.read_text())
    record["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(days=store.STORE_TTL_DAYS - 1)
    ).isoformat()
    path.write_text(json.dumps(record))

    assert store.get(store.OFF_PRODUCT, "1") == {"a": 1}


def test_a_naive_timestamp_is_refused():
    """Without an offset we cannot know when it was fetched, so we must not
    pretend to."""
    store.put(store.OFF_PRODUCT, "1", {"a": 1})

    path = store.path_for(store.OFF_PRODUCT, "1")
    record = json.loads(path.read_text())
    record["fetched_at"] = datetime.now().isoformat()      # no tzinfo
    path.write_text(json.dumps(record))

    assert store.get(store.OFF_PRODUCT, "1") is None


# ══ Robustness ════════════════════════════════════════════════════════

def test_a_corrupt_record_is_ignored_not_fatal():
    """A bad file in the cache must not fail a request that would otherwise
    have succeeded — this is an optimisation, not a source of truth."""
    path = store.path_for(store.OFF_PRODUCT, "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")

    assert store.get(store.OFF_PRODUCT, "1") is None


def test_a_write_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", Path("/proc/nonexistent/nope"))

    store.put(store.OFF_PRODUCT, "1", {"a": 1})      # must not raise


def test_a_hostile_key_cannot_escape_the_store():
    """Keys are barcodes, but they arrive from the network."""
    store.put(store.OFF_PRODUCT, "../../../etc/passwd", {"a": 1})

    written = list(store.STORE_DIR.rglob("*.json"))
    assert written
    for path in written:
        assert store.STORE_DIR in path.parents or path.is_relative_to(store.STORE_DIR)


def test_writes_leave_no_temporary_files_behind():
    store.put(store.OFF_PRODUCT, "1", {"a": 1})

    assert list(store.STORE_DIR.rglob("*.tmp")) == []


def test_the_store_can_be_disabled(monkeypatch):
    monkeypatch.setattr(store, "STORE_ENABLED", False)

    store.put(store.OFF_PRODUCT, "1", {"a": 1})
    assert store.get(store.OFF_PRODUCT, "1") is None


# ══ The point: a hit costs no request and no budget ════════════════════

class _FakeOffSdk:
    def __init__(self):
        self.calls = []
        outer = self

        class _Product:
            def get(self, barcode, fields=None):
                outer.calls.append(barcode)
                return {"code": barcode, "product_name": "Nutella", "nutriments": {}}

        self.product = _Product()


async def test_a_stored_response_costs_no_upstream_call(monkeypatch):
    sdk = _FakeOffSdk()
    monkeypatch.setattr(off, "_get_off_api", lambda: sdk)

    first = await off.get_product("3017620422003")
    second = await off.get_product("3017620422003")

    assert first == second
    assert sdk.calls == ["3017620422003"]      # the upstream was asked once


async def test_a_stored_response_costs_no_rate_limit_budget(monkeypatch):
    """The whole point. Open Food Facts allows fifteen requests a minute; a
    barcode we already hold must not spend one of them."""
    sdk = _FakeOffSdk()
    monkeypatch.setattr(off, "_get_off_api", lambda: sdk)

    await off.get_product("3017620422003")           # populates the store
    before = ratelimit.off_limiter.tokens

    await off.get_product("3017620422003")           # served from disk

    assert ratelimit.off_limiter.tokens == pytest.approx(before, abs=0.01)


async def test_the_store_survives_the_in_memory_cache_being_lost(monkeypatch):
    """A restart empties the in-memory cache. Before the store, every deploy
    re-spent the budget on barcodes we had already fetched."""
    from app.core import orchestrator

    sdk = _FakeOffSdk()
    monkeypatch.setattr(off, "_get_off_api", lambda: sdk)

    async def usda_none(upc, *a, **k):
        return None

    async def no_gpc(categories):
        return [], 0.0

    monkeypatch.setattr(usda_fdc, "search_by_upc", usda_none)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", no_gpc)

    await orchestrator.lookup("3017620422003")
    orchestrator._lookup_cache.clear()               # as a restart would
    await orchestrator.lookup("3017620422003")

    assert sdk.calls == ["3017620422003"]            # still only asked once


async def test_a_known_barcode_skips_the_usda_search(monkeypatch):
    """FDC has no barcode endpoint, so a lookup costs a fuzzy search *and* a
    fetch. The barcode's id does not change, so remembering it removes the
    search — the call that spends budget and that FDC answers imprecisely."""
    searches = []

    class Client:
        def search(self, query, data_type=None, page_size=50, **kw):
            searches.append(query)

            class Food:
                fdc_id = 1603628
                gtin_upc = "028400642255"

            class R:
                foods = [Food()]
            return R()

        def get_food(self, fdc_id):
            class N:
                id, name, amount, unit_name = 1008, "Energy", 536.0, "KCAL"

            class F:
                fdc_id_ = fdc_id
                description = "DORITOS"
                data_type = "Branded"
                brand_owner = brand_name = ingredients = None
                serving_size = serving_size_unit = None
                nutrients = [N()]
            f = F()
            f.fdc_id = fdc_id
            return f

    monkeypatch.setattr(usda_fdc, "_get_fdc_client", lambda: Client())

    await usda_fdc.search_by_upc("028400642255")
    await usda_fdc.search_by_upc("028400642255")

    assert searches == ["028400642255"]              # searched once, not twice


# ══ The SQLite importer ═══════════════════════════════════════════════

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A small store on disk, as the service would have written it."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "responses")

    store.put(store.OFF_PRODUCT, "3017620422003", {
        "code": "3017620422003", "product_name": "Nutella", "brands": "Ferrero",
        "image_url": "https://img.example/n.jpg",
    })
    store.put(store.USDA_FOOD, "2500691", {
        "fdc_id": 2500691, "description": "CHOCOLATE SANDWICH COOKIES",
        "brand_owner": "Mondelez", "data_type": "Branded",
        "nutrients": [
            {"id": 1008, "name": "Energy", "amount": 471.0, "unit": "kcal"},
            {"id": 1062, "name": "Energy", "amount": 1971.0, "unit": "kJ"},
            {"id": 1093, "name": "Sodium, Na", "amount": 382.0, "unit": "mg"},
        ],
    })
    store.put(store.USDA_UPC, "00044000032029", 2500691)
    return tmp_path


def test_the_importer_builds_a_queryable_database(corpus, tmp_path):
    db = tmp_path / "responses.sqlite3"

    counts = importer.build(store.STORE_DIR, db)

    assert counts["off_products"] == 1
    assert counts["usda_foods"] == 1
    assert counts["usda_nutrients"] == 3
    assert counts["usda_upc_map"] == 1
    assert counts["skipped"] == 0

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT product_name FROM off_products WHERE barcode = '3017620422003'"
    ).fetchone()[0] == "Nutella"
    assert conn.execute(
        "SELECT fdc_id FROM usda_upc_map WHERE gtin14 = '00044000032029'"
    ).fetchone()[0] == 2500691
    conn.close()


def test_nutrients_are_keyed_by_id_so_energy_does_not_collapse(corpus, tmp_path):
    """Both energy rows survive, distinguishable. Keyed by name, the kJ row
    would overwrite the kcal one — which is how cheddar came to be served at
    1710 kcal."""
    db = tmp_path / "responses.sqlite3"
    importer.build(store.STORE_DIR, db)

    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT nutrient_id, amount FROM usda_nutrients WHERE name = 'Energy'"
    ).fetchall())
    conn.close()

    assert rows == {1008: 471.0, 1062: 1971.0}


def test_the_payload_is_preserved_verbatim(corpus, tmp_path):
    """A flattened column can be wrong or go out of date. The payload it came
    from cannot."""
    db = tmp_path / "responses.sqlite3"
    importer.build(store.STORE_DIR, db)

    conn = sqlite3.connect(db)
    payload = json.loads(conn.execute(
        "SELECT payload FROM off_products WHERE barcode = '3017620422003'"
    ).fetchone()[0])
    conn.close()

    assert payload["brands"] == "Ferrero"
    assert payload["code"] == "3017620422003"


def test_imported_timestamps_are_utc(corpus, tmp_path):
    db = tmp_path / "responses.sqlite3"
    importer.build(store.STORE_DIR, db)

    conn = sqlite3.connect(db)
    fetched_at = conn.execute(
        "SELECT fetched_at FROM off_products LIMIT 1"
    ).fetchone()[0]
    imported_at = conn.execute(
        "SELECT value FROM store_metadata WHERE key = 'imported_at'"
    ).fetchone()[0]
    conn.close()

    for stamp in (fetched_at, imported_at):
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


def test_timestamps_sort_correctly_as_text(corpus, tmp_path):
    """SQLite compares text lexically. ISO-8601 in UTC sorts correctly under
    that; a local timestamp would not."""
    db = tmp_path / "responses.sqlite3"
    importer.build(store.STORE_DIR, db)

    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO off_products (barcode, fetched_at, payload)
           VALUES ('1', '2020-01-01T00:00:00+00:00', '{}')""",
    )
    conn.execute(
        """INSERT INTO off_products (barcode, fetched_at, payload)
           VALUES ('2', '2030-01-01T00:00:00+00:00', '{}')""",
    )
    newest = conn.execute(
        "SELECT barcode FROM off_products ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()

    assert newest == "2"


def test_the_import_is_idempotent(corpus, tmp_path):
    db = tmp_path / "responses.sqlite3"

    importer.build(store.STORE_DIR, db)
    first = importer.build(store.STORE_DIR, db)

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM off_products").fetchone()[0] == 1
    conn.close()
    assert first["off_products"] == 1


def test_a_corrupt_record_is_skipped_not_fatal(corpus, tmp_path):
    bad = store.STORE_DIR / store.OFF_PRODUCT / "zz" / "zz" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not json")

    counts = importer.build(store.STORE_DIR, tmp_path / "responses.sqlite3")

    assert counts["off_products"] == 1          # the good record still imported


def test_the_import_is_atomic(corpus, tmp_path, monkeypatch):
    """A failed rebuild must leave the previous database intact, not a
    half-written one."""
    db = tmp_path / "responses.sqlite3"
    importer.build(store.STORE_DIR, db)
    before = db.read_bytes()

    def explode(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(importer.os, "replace", explode)

    with pytest.raises(OSError):
        importer.build(store.STORE_DIR, db)

    assert db.read_bytes() == before
