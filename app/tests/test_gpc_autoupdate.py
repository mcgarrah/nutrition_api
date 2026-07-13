"""
Tests for the importer's --auto-update decision logic and XML resolution.

This is the code that decides, on every boot, whether to re-download the
taxonomy from GS1. Getting it wrong is expensive in both directions: too eager
and we hammer GS1 and rebuild a 27 MB database on every restart; too lazy and
the taxonomy silently goes stale forever.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_gpc_xml as importer  # noqa: E402

from app.tests.test_gpc_importer import FOOD_XML  # noqa: E402


@pytest.fixture
def seeded_db(tmp_path):
    """A database already imported at version 20251127."""
    xml = tmp_path / "en-v20251127.xml"
    xml.write_text(FOOD_XML)
    db = tmp_path / "gpc.sqlite3"
    importer.import_food_gpc(str(xml), db)
    return db


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Invoke main() with argv, recording whether an import happened."""
    def run(argv, remote=None, check_remote=True, imported=None):
        calls = {"imported": [], "resolved": 0}

        monkeypatch.setattr(sys, "argv", ["import_gpc_xml.py", *argv])
        monkeypatch.setattr(importer, "get_latest_remote_version", lambda: remote)
        monkeypatch.setattr(importer, "should_check_remote", lambda db: check_remote)
        monkeypatch.setattr(importer, "set_last_version_check", lambda db: None)

        def fake_resolve(args):
            calls["resolved"] += 1
            xml = tmp_path / "en-v20260520.xml"
            xml.write_text(FOOD_XML)
            return str(xml)

        def fake_import(xml_path, db_path):
            calls["imported"].append((xml_path, db_path))
            # main() reports row counts afterwards, so the schema must exist
            conn = sqlite3.connect(db_path)
            conn.executescript(importer.SCHEMA)
            conn.commit()
            conn.close()
            return dict.fromkeys(
                ["segments", "families", "classes", "bricks", "attribute_types",
                 "attribute_values", "brick_attribute_types", "attribute_type_values"], 0,
            )

        monkeypatch.setattr(importer, "resolve_xml_file", fake_resolve)
        monkeypatch.setattr(importer, "import_food_gpc", fake_import)
        importer.main()
        return calls

    return run


# ── --auto-update: when to skip ───────────────────────────────────────

def test_skips_when_local_version_matches_remote(seeded_db, run_main):
    calls = run_main(["--auto-update", "--db", str(seeded_db)], remote="v20251127")
    assert calls["imported"] == []


def test_skips_when_local_is_newer_than_remote(seeded_db, run_main):
    """A hand-placed newer XML must not be clobbered by an older GS1 release."""
    calls = run_main(["--auto-update", "--db", str(seeded_db)], remote="v20240101")
    assert calls["imported"] == []


def test_skips_when_rate_limited(seeded_db, run_main):
    """Checked GS1 recently: don't even ask again."""
    calls = run_main(
        ["--auto-update", "--db", str(seeded_db)], remote="v20260520", check_remote=False,
    )
    assert calls["imported"] == []


def test_skips_when_remote_is_unreachable(seeded_db, run_main):
    """GS1 down (it 403s from some networks): keep serving existing data."""
    calls = run_main(["--auto-update", "--db", str(seeded_db)], remote=None)
    assert calls["imported"] == []


# ── --auto-update: when to import ─────────────────────────────────────

def test_updates_when_remote_is_newer(seeded_db, run_main):
    calls = run_main(["--auto-update", "--db", str(seeded_db)], remote="v20260520")
    assert len(calls["imported"]) == 1


def test_builds_from_scratch_when_no_database_exists(tmp_path, run_main):
    """First boot: nothing to compare against, so just build."""
    calls = run_main(["--auto-update", "--db", str(tmp_path / "absent.sqlite3")])
    assert len(calls["imported"]) == 1


def test_version_check_is_recorded_even_when_the_check_fails(seeded_db, monkeypatch, tmp_path):
    """The rate-limit timestamp must be written regardless of outcome, or a
    broken GS1 means we retry on every single restart."""
    recorded = []

    monkeypatch.setattr(sys, "argv",
                        ["import_gpc_xml.py", "--auto-update", "--db", str(seeded_db)])
    monkeypatch.setattr(importer, "get_latest_remote_version", lambda: None)
    monkeypatch.setattr(importer, "should_check_remote", lambda db: True)
    monkeypatch.setattr(importer, "set_last_version_check", lambda db: recorded.append(db))

    importer.main()

    assert recorded == [seeded_db]


# ── plain (non-auto-update) import ────────────────────────────────────

def test_plain_run_always_imports(tmp_path, run_main):
    calls = run_main(["--db", str(tmp_path / "gpc.sqlite3")])
    assert len(calls["imported"]) == 1


# ── resolve_xml_file ──────────────────────────────────────────────────

class _Args:
    def __init__(self, xml=None, download=False):
        self.xml = xml
        self.download = download


def test_resolve_prefers_a_downloaded_file(monkeypatch, tmp_path):
    downloaded = tmp_path / "en-v20260520.xml"
    downloaded.write_text(FOOD_XML)

    class FakeDownloader:
        def __init__(self, **kw):
            pass

        def download_latest_gpc_xml(self):
            return str(downloaded)

        def find_latest_xml_file(self):
            return None

    monkeypatch.setattr("gs1_gpc.downloader.GPCDownloader", FakeDownloader)
    assert importer.resolve_xml_file(_Args(download=True)) == str(downloaded)


def test_resolve_falls_back_to_cache_when_download_fails(monkeypatch, tmp_path):
    cached = tmp_path / "en-v20251127.xml"
    cached.write_text(FOOD_XML)

    class FakeDownloader:
        def __init__(self, **kw):
            pass

        def download_latest_gpc_xml(self):
            return None            # download failed

        def find_latest_xml_file(self):
            return str(cached)

    monkeypatch.setattr("gs1_gpc.downloader.GPCDownloader", FakeDownloader)
    assert importer.resolve_xml_file(_Args(download=True)) == str(cached)


def test_resolve_uses_cache_without_downloading(monkeypatch, tmp_path):
    cached = tmp_path / "en-v20251127.xml"
    cached.write_text(FOOD_XML)
    downloads = {"n": 0}

    class FakeDownloader:
        def __init__(self, **kw):
            pass

        def download_latest_gpc_xml(self):
            downloads["n"] += 1
            return None

        def find_latest_xml_file(self):
            return str(cached)

    monkeypatch.setattr("gs1_gpc.downloader.GPCDownloader", FakeDownloader)
    result = importer.resolve_xml_file(_Args(download=False))

    assert result == str(cached)
    assert downloads["n"] == 0     # no network call when not asked for one


def test_resolve_falls_back_to_bundled_xml_when_downloader_raises(monkeypatch):
    """gs1_gpc raising (or absent) must not be fatal — we ship an XML."""
    class Boom:
        def __init__(self, **kw):
            raise RuntimeError("GS1 unreachable")

    monkeypatch.setattr("gs1_gpc.downloader.GPCDownloader", Boom)
    monkeypatch.setattr(importer, "LOCAL_XML", Path("/definitely/missing.xml"))

    with pytest.raises(SystemExit):
        importer.resolve_xml_file(_Args())


def test_resolve_exits_when_nothing_is_available(monkeypatch, tmp_path):
    class FakeDownloader:
        def __init__(self, **kw):
            pass

        def download_latest_gpc_xml(self):
            return None

        def find_latest_xml_file(self):
            return None

    monkeypatch.setattr("gs1_gpc.downloader.GPCDownloader", FakeDownloader)
    monkeypatch.setattr(importer, "LOCAL_XML", tmp_path / "missing.xml")

    with pytest.raises(SystemExit):
        importer.resolve_xml_file(_Args())


def test_database_with_unknown_version_still_updates(tmp_path, run_main, monkeypatch):
    """End-to-end guard for the auto-update killer: a DB whose version didn't
    parse must not conclude it is current and freeze forever."""
    xml = tmp_path / "gpc_cached.xml"   # no -v in the name
    xml.write_text(FOOD_XML.replace('dateUtc="2/12/2024"', 'dateUtc="bogus"'))
    db = tmp_path / "gpc.sqlite3"
    importer.import_food_gpc(str(xml), db)
    assert importer.get_stored_version(db) == "unknown"

    calls = run_main(["--auto-update", "--db", str(db)], remote="v20260520")

    assert len(calls["imported"]) == 1
