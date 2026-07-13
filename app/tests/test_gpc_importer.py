"""
Tests for scripts/import_gpc_xml.py — the GS1 GPC XML → SQLite importer.

The importer is the reason this project exists: the Django prototypes it
replaces used single foreign keys and lost data when one attribute type
appeared on several bricks. These tests pin the junction-table behaviour
that fixes that, plus the food-segment filter and the version/auto-update
bookkeeping that decides whether we re-download from GS1.

No network access: the GS1 crawler is stubbed.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import datetime
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_gpc_xml as importer  # noqa: E402


# ── XML fixtures ──────────────────────────────────────────────────────

FOOD_XML = """<?xml version="1.0" encoding="utf-8"?>
<schema languageCode="EN" dateUtc="2/12/2024">
  <segment code="50000000" text="Food/Beverage">
    <family code="50200000" text="Beverages">
      <class code="50202300" text="Carbonated Drinks">
        <brick code="10000201" text="Cola Drinks">
          <attType code="20000100" text="Caffeine Presence">
            <attValue code="30000101" text="Caffeinated"/>
            <attValue code="30000102" text="Decaffeinated"/>
          </attType>
        </brick>
        <brick code="10000202" text="Lemonade">
          <!-- same attType as Cola Drinks: the many-to-many case -->
          <attType code="20000100" text="Caffeine Presence">
            <attValue code="30000101" text="Caffeinated"/>
          </attType>
          <attType code="20000200" text="Sweetener Type">
            <attValue code="30000201" text="Sugar"/>
          </attType>
        </brick>
      </class>
    </family>
  </segment>
  <segment code="70000000" text="Arts/Crafts/Needlework">
    <family code="70010000" text="Arts/Crafts Supplies">
      <class code="70010100" text="Paint">
        <brick code="10000999" text="Acrylic Paint"/>
      </class>
    </family>
  </segment>
</schema>
"""


@pytest.fixture
def food_xml(tmp_path):
    path = tmp_path / "en-v20251127.xml"
    path.write_text(FOOD_XML)
    return path


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "gpc.sqlite3"


def rows(db, sql):
    conn = sqlite3.connect(db)
    out = conn.execute(sql).fetchall()
    conn.close()
    return out


def scalar(db, sql):
    return rows(db, sql)[0][0]


# ── import_food_gpc: the hierarchy ────────────────────────────────────

def test_imports_full_hierarchy(food_xml, db_path):
    counts = importer.import_food_gpc(str(food_xml), db_path)

    assert counts["segments"] == 1
    assert counts["families"] == 1
    assert counts["classes"] == 1
    assert counts["bricks"] == 2
    assert scalar(db_path, "SELECT description FROM segments") == "Food/Beverage"
    assert scalar(db_path, "SELECT description FROM families") == "Beverages"


def test_non_food_segments_are_excluded(food_xml, db_path):
    """Only segment 50000000 is imported — the other 43 GPC segments are noise."""
    importer.import_food_gpc(str(food_xml), db_path)

    assert rows(db_path, "SELECT segment_code FROM segments") == [("50000000",)]
    # The Arts/Crafts brick and its whole subtree must be absent
    assert rows(db_path, "SELECT brick_code FROM bricks WHERE brick_code = '10000999'") == []
    assert scalar(db_path, "SELECT COUNT(*) FROM families") == 1


def test_foreign_keys_link_the_hierarchy(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)

    row = rows(db_path, """
        SELECT s.description, f.description, c.description, b.description
        FROM bricks b
        JOIN classes c ON b.class_code = c.class_code
        JOIN families f ON c.family_code = f.family_code
        JOIN segments s ON f.segment_code = s.segment_code
        WHERE b.brick_code = '10000201'
    """)[0]
    assert row == ("Food/Beverage", "Beverages", "Carbonated Drinks", "Cola Drinks")


# ── import_food_gpc: the junction tables (the point of the rewrite) ────

def test_shared_attribute_type_is_stored_once_but_linked_to_both_bricks():
    """The bug this schema exists to fix.

    attType 20000100 appears on two bricks. A single-FK schema would keep
    only the last one. The junction table must keep both links while storing
    the attribute type itself exactly once.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        xml = tmp / "en-v20251127.xml"
        xml.write_text(FOOD_XML)
        db = tmp / "gpc.sqlite3"

        importer.import_food_gpc(str(xml), db)

        # Stored once...
        assert scalar(
            db, "SELECT COUNT(*) FROM attribute_types WHERE att_type_code='20000100'"
        ) == 1
        # ...but linked to both bricks
        linked = rows(db, """
            SELECT brick_code FROM brick_attribute_types
            WHERE att_type_code = '20000100' ORDER BY brick_code
        """)
        assert linked == [("10000201",), ("10000202",)]


def test_attribute_values_deduplicate_across_bricks(food_xml, db_path):
    """attValue 30000101 appears under two bricks — stored once, linked once."""
    importer.import_food_gpc(str(food_xml), db_path)

    assert scalar(
        db_path, "SELECT COUNT(*) FROM attribute_values WHERE att_value_code='30000101'"
    ) == 1
    assert scalar(db_path, """
        SELECT COUNT(*) FROM attribute_type_values
        WHERE att_type_code='20000100' AND att_value_code='30000101'
    """) == 1


def test_counts_report_occurrences_not_unique_rows(food_xml, db_path):
    """The returned counts are XML occurrences; the DB deduplicates them."""
    counts = importer.import_food_gpc(str(food_xml), db_path)

    assert counts["attribute_types"] == 3          # 1 + 2 occurrences in the XML
    assert scalar(db_path, "SELECT COUNT(*) FROM attribute_types") == 2  # unique


def test_brick_with_no_attributes_still_imports(tmp_path):
    xml = tmp_path / "en-v20251127.xml"
    xml.write_text("""<?xml version="1.0"?>
<schema dateUtc="2/12/2024">
  <segment code="50000000" text="Food/Beverage">
    <family code="50200000" text="Beverages">
      <class code="50202300" text="Carbonated Drinks">
        <brick code="10000201" text="Cola Drinks"/>
      </class>
    </family>
  </segment>
</schema>""")
    db = tmp_path / "gpc.sqlite3"

    counts = importer.import_food_gpc(str(xml), db)

    assert counts["bricks"] == 1
    assert scalar(db, "SELECT COUNT(*) FROM brick_attribute_types") == 0


# ── import_food_gpc: metadata & idempotency ───────────────────────────

def test_writes_metadata(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)

    meta = dict(rows(db_path, "SELECT key, value FROM gpc_metadata"))
    assert meta["gpc_version"] == "20251127"      # from the -v filename
    assert meta["xml_date"] == "2/12/2024"        # raw XML attribute
    assert meta["food_segments"] == "50000000"
    assert meta["xml_source"] == str(food_xml)
    # import_timestamp must be a parseable UTC ISO timestamp
    assert datetime.datetime.fromisoformat(meta["import_timestamp"]).tzinfo is not None


def test_reimport_replaces_the_database(food_xml, db_path):
    """A re-import must not accumulate duplicate rows."""
    importer.import_food_gpc(str(food_xml), db_path)
    first = scalar(db_path, "SELECT COUNT(*) FROM bricks")

    importer.import_food_gpc(str(food_xml), db_path)
    second = scalar(db_path, "SELECT COUNT(*) FROM bricks")

    assert first == second == 2


def test_creates_parent_directory(tmp_path, food_xml):
    nested = tmp_path / "deep" / "nested" / "gpc.sqlite3"
    importer.import_food_gpc(str(food_xml), nested)
    assert nested.exists()


# ── extract_version_from_path ─────────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    ("en-v20251127.xml", "20251127"),        # GPCDownloader naming
    ("en-v20260520.xml", "20260520"),
    ("gpc-20240101.xml", "20240101"),        # bare "-<digits>" form
])
def test_version_from_filename(filename, expected):
    assert importer.extract_version_from_path(filename, "unknown") == expected


def test_version_prefers_filename_over_xml_date():
    assert importer.extract_version_from_path("en-v20251127.xml", "1/2/2020") == "20251127"


def test_version_unknown_when_nothing_parses():
    assert importer.extract_version_from_path("gpc_november_2024.xml", "unknown") == "unknown"
    assert importer.extract_version_from_path("gpc_november_2024.xml", "not-a-date") == "unknown"


# ── get_stored_version ────────────────────────────────────────────────

def test_stored_version_none_when_db_missing(tmp_path):
    assert importer.get_stored_version(tmp_path / "nope.sqlite3") is None


def test_stored_version_none_when_table_missing(tmp_path):
    db = tmp_path / "empty.sqlite3"
    sqlite3.connect(db).close()
    assert importer.get_stored_version(db) is None


def test_stored_version_read_back(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)
    assert importer.get_stored_version(db_path) == "20251127"


# ── version-check rate limiting ───────────────────────────────────────

def test_should_check_remote_when_never_checked(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)
    assert importer.get_last_version_check(db_path) is None
    assert importer.should_check_remote(db_path) is True


def test_recording_a_check_suppresses_the_next_one(food_xml, db_path):
    """Rate limit: don't hammer GS1 on every restart."""
    importer.import_food_gpc(str(food_xml), db_path)
    importer.set_last_version_check(db_path)

    assert importer.get_last_version_check(db_path) is not None
    assert importer.should_check_remote(db_path) is False


def test_should_check_remote_again_once_the_interval_elapses(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=importer.VERSION_CHECK_INTERVAL_SECONDS + 60
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO gpc_metadata VALUES ('last_version_check', ?)",
        (stale.isoformat(),),
    )
    conn.commit()
    conn.close()

    assert importer.should_check_remote(db_path) is True


def test_corrupt_timestamp_falls_back_to_checking(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO gpc_metadata VALUES ('last_version_check', 'garbage')")
    conn.commit()
    conn.close()

    assert importer.should_check_remote(db_path) is True


def test_set_last_version_check_on_missing_db_is_harmless(tmp_path):
    """Non-critical bookkeeping must never raise."""
    importer.set_last_version_check(tmp_path / "nope.sqlite3")  # must not raise


# ── get_latest_remote_version (GS1 network boundary, stubbed) ─────────

def test_remote_version_returns_latest_publication(monkeypatch):
    class Pub:
        version = "v20260520"

    async def fake_get_language(code):
        return {"code": code}

    async def fake_get_publications(lang):
        return [Pub()]

    import gpcc._crawlers as crawlers
    monkeypatch.setattr(crawlers, "get_language", fake_get_language)
    monkeypatch.setattr(crawlers, "get_publications", fake_get_publications)

    assert importer.get_latest_remote_version() == "v20260520"


def test_remote_version_none_when_no_publications(monkeypatch):
    async def fake_get_language(code):
        return {"code": code}

    async def fake_get_publications(lang):
        return []

    import gpcc._crawlers as crawlers
    monkeypatch.setattr(crawlers, "get_language", fake_get_language)
    monkeypatch.setattr(crawlers, "get_publications", fake_get_publications)

    assert importer.get_latest_remote_version() is None


def test_remote_version_none_when_gs1_errors(monkeypatch):
    """GS1 returning 403 (as it does from some networks) must not raise."""
    async def boom(code):
        raise RuntimeError("403 Forbidden")

    import gpcc._crawlers as crawlers
    monkeypatch.setattr(crawlers, "get_language", boom)

    assert importer.get_latest_remote_version() is None


# ── resolve_xml_file ──────────────────────────────────────────────────

class _Args:
    def __init__(self, xml=None, download=False):
        self.xml = xml
        self.download = download


def test_resolve_xml_uses_explicit_path(food_xml):
    assert importer.resolve_xml_file(_Args(xml=food_xml)) == str(food_xml)


def test_resolve_xml_exits_when_explicit_path_missing(tmp_path):
    with pytest.raises(SystemExit):
        importer.resolve_xml_file(_Args(xml=tmp_path / "missing.xml"))


def test_remote_version_none_on_timeout(monkeypatch):
    """GS1 hanging must not block startup — the check gives up and returns None."""
    import asyncio

    async def hangs(code):
        raise asyncio.TimeoutError()

    import gpcc._crawlers as crawlers
    monkeypatch.setattr(crawlers, "get_language", hangs)

    assert importer.get_latest_remote_version() is None
