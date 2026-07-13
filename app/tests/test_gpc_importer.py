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

    import gpcc
    monkeypatch.setattr(gpcc, "get_language", fake_get_language)
    monkeypatch.setattr(gpcc, "get_publications", fake_get_publications)

    assert importer.get_latest_remote_version() == "v20260520"


def test_remote_version_none_when_no_publications(monkeypatch):
    async def fake_get_language(code):
        return {"code": code}

    async def fake_get_publications(lang):
        return []

    import gpcc
    monkeypatch.setattr(gpcc, "get_language", fake_get_language)
    monkeypatch.setattr(gpcc, "get_publications", fake_get_publications)

    assert importer.get_latest_remote_version() is None


def test_remote_version_none_when_gs1_errors(monkeypatch):
    """GS1 returning 403 (as it does from some networks) must not raise."""
    async def boom(code):
        raise RuntimeError("403 Forbidden")

    import gpcc
    monkeypatch.setattr(gpcc, "get_language", boom)

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

    import gpcc
    monkeypatch.setattr(gpcc, "get_language", hangs)

    assert importer.get_latest_remote_version() is None


# ── date parsing: GS1 publishes D/M/YYYY, not M/D/YYYY ────────────────

@pytest.mark.parametrize("xml_date,expected", [
    ("27/11/2025", "20251127"),   # 27 November 2025 — day > 12, unambiguous
    ("2/12/2024", "20241202"),    # 2 December 2024 — the bundled fallback XML
    ("01/01/2026", "20260101"),
])
def test_xml_date_is_parsed_as_day_first(xml_date, expected):
    """GS1's dateUtc is D/M/YYYY. Reading it as M/D/YYYY silently shifts the
    version by months, or fails outright once the day exceeds 12."""
    assert importer.extract_version_from_path("gpc_cached.xml", xml_date) == expected


def test_unparseable_date_yields_unknown():
    assert importer.extract_version_from_path("gpc_cached.xml", "13/13/2025") == "unknown"


# ── version comparison ────────────────────────────────────────────────

@pytest.mark.parametrize("remote,stored,expected", [
    ("v20260520", "20251127", True),    # remote is newer
    ("20260520", "20251127", True),     # the 'v' prefix is optional
    ("v20251127", "20251127", False),   # same version
    ("v20240101", "20251127", False),   # remote is older — never downgrade
])
def test_is_remote_newer(remote, stored, expected):
    assert importer.is_remote_newer(remote, stored) is expected


def test_unknown_stored_version_takes_the_remote():
    """The auto-update killer.

    A lexical compare puts "unknown" above any date ('u' > '2'), so a database
    whose version failed to parse would decide it was already current and never
    update again. An unusable local version must defer to the remote instead.
    """
    assert importer.is_remote_newer("v20260520", "unknown") is True
    assert importer.is_remote_newer("v20260520", None) is True


def test_unusable_remote_version_is_ignored():
    """Symmetrically: never rebuild the database on a garbage remote version."""
    assert importer.is_remote_newer("garbage", "20251127") is False
    assert importer.is_remote_newer(None, "20251127") is False


# ── gpcc is used through its public API ───────────────────────────────

def test_gs1_version_check_uses_the_public_gpcc_api():
    """We import get_language/get_publications from the gpcc package root.

    They were previously taken from gpcc._crawlers — a private module — for no
    benefit: gpcc re-exports both and lists them in __all__. A private module
    can be renamed in a patch release and silently break the auto-update.
    """
    import inspect

    source = inspect.getsource(importer.get_latest_remote_version)

    assert "from gpcc import get_language, get_publications" in source
    assert "from gpcc._crawlers import" not in source


def test_gpcc_still_exports_what_we_import():
    """Canary on the dependency: if gpcc stops exporting these, the GS1
    version check breaks and the taxonomy quietly stops auto-updating."""
    import gpcc

    assert "get_language" in gpcc.__all__
    assert "get_publications" in gpcc.__all__
    assert callable(gpcc.get_language)
    assert callable(gpcc.get_publications)


def test_gpcc_is_a_declared_dependency():
    """We import gpcc directly, so it must be declared directly.

    It arrives transitively via gs1-gpc today; if that ever changes, an
    undeclared import breaks with no warning from the requirements file.
    """
    from pathlib import Path

    requirements = (
        Path(__file__).resolve().parents[2] / "requirements.txt"
    ).read_text()

    assert "gpcc" in requirements


# ── Atomic build & cross-process locking ──────────────────────────────
# Every uvicorn worker runs the startup lifespan, so `--workers 2` means two
# processes reach the importer at the same boot. It used to unlink the live
# database and rebuild it in place, which meant: a worker already serving held
# an open handle to a deleted inode, a second importer hit "disk I/O error"
# mid-write, and a crash part-way left a half-built database that the next boot
# mistook for a good one.

def test_import_is_atomic_no_partial_database_is_ever_visible(food_xml, db_path, monkeypatch):
    """A failure part-way through must leave the previous database untouched,
    not a half-written one that looks valid."""
    importer.import_food_gpc(str(food_xml), db_path)
    good_bricks = scalar(db_path, "SELECT COUNT(*) FROM bricks")
    assert good_bricks == 2

    # Blow up after the schema is created but before the swap
    real_replace = importer.os.replace

    def explode(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(importer.os, "replace", explode)

    with pytest.raises(OSError):
        importer.import_food_gpc(str(food_xml), db_path)

    monkeypatch.setattr(importer.os, "replace", real_replace)

    # The original database is still intact and complete
    assert scalar(db_path, "SELECT COUNT(*) FROM bricks") == good_bricks
    assert rows(db_path, "PRAGMA integrity_check")[0][0] == "ok"


def test_import_does_not_unlink_the_live_database(food_xml, db_path):
    """The old code deleted the target first. A worker already serving from it
    kept an open handle to the deleted inode."""
    importer.import_food_gpc(str(food_xml), db_path)
    original_inode = db_path.stat().st_ino

    importer.import_food_gpc(str(food_xml), db_path)

    # The file was replaced atomically, so it is a *new* inode — the old one
    # was never truncated out from under an open reader.
    assert db_path.stat().st_ino != original_inode
    assert scalar(db_path, "SELECT COUNT(*) FROM bricks") == 2


def test_import_leaves_no_temporary_files_behind(food_xml, db_path):
    importer.import_food_gpc(str(food_xml), db_path)

    leftovers = list(db_path.parent.glob("*.building-*"))
    assert leftovers == []


def test_import_lock_serializes_two_processes(tmp_path):
    """The lock must actually exclude — not merely exist."""
    import multiprocessing
    import time

    db = tmp_path / "gpc.sqlite3"
    order = multiprocessing.Manager().list()

    def hold_lock(tag, hold_s):
        with importer.import_lock(db):
            order.append(f"{tag}-enter")
            time.sleep(hold_s)
            order.append(f"{tag}-exit")

    first = multiprocessing.Process(target=hold_lock, args=("a", 0.4))
    first.start()
    time.sleep(0.1)                       # let it take the lock
    second = multiprocessing.Process(target=hold_lock, args=("b", 0.0))
    second.start()

    first.join(timeout=10)
    second.join(timeout=10)

    # b must not have entered while a was inside
    assert list(order) == ["a-enter", "a-exit", "b-enter", "b-exit"]


def test_import_lock_is_reentrant_across_sequential_calls(tmp_path):
    db = tmp_path / "gpc.sqlite3"
    with importer.import_lock(db):
        pass
    with importer.import_lock(db):
        pass    # must not deadlock on the second acquisition
