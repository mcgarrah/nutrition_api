"""
Tests for scripts/refresh_mirrors.py — the scheduled OFF/FDC rebuild loop.

subprocess.run is faked throughout: no real build script, no real network,
no real `gh`/`sudo`. The fake's "build" branch writes directly into the
mirror's sqlite file, simulating what a real build_off_db.py/build_fdc_db.py
--auto-update run would have left behind, so refresh_one()'s before/after
comparison exercises real sqlite reads against a real (if tiny) database.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import refresh_mirrors as rm  # noqa: E402


def _write_db(path: Path, table: str, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT INTO {table} VALUES (?, ?)",
        [(k, str(v)) for k, v in metadata.items()],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def mirror(tmp_path):
    db_path = tmp_path / "off.sqlite3"
    archive_path = tmp_path / "off.sqlite3.xz"
    _write_db(db_path, "off_metadata", {"dataset": "off-2026-07-17", "products": 100})
    archive_path.write_bytes(b"fake archive")
    return rm.Mirror(
        name="Open Food Facts",
        build_script=Path("build_off_db.py"),
        db_path=db_path,
        archive_path=archive_path,
        metadata_table="off_metadata",
        count_key="products",
        release_title_prefix="OFF products —",
        release_body=rm._off_release_body,
    )


class FakeRuns:
    """Records every subprocess.run call; the "build" branch can be told to
    rewrite the mirror's db (a real rebuild), leave it untouched (an
    --auto-update no-op), or raise (a failed build)."""

    def __init__(self, mirror, new_metadata=None, build_fails=False, gh_tag_exists=False):
        self.calls = []
        self.mirror = mirror
        self.new_metadata = new_metadata
        self.build_fails = build_fails
        self.gh_tag_exists = gh_tag_exists

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if str(cmd[1]).endswith(".py"):
            if self.build_fails:
                raise subprocess.CalledProcessError(1, cmd)
            if self.new_metadata is not None:
                _write_db(self.mirror.db_path, self.mirror.metadata_table, self.new_metadata)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(cmd, 0 if self.gh_tag_exists else 1)
        return subprocess.CompletedProcess(cmd, 0)


# ── _metadata / _backup / _restore / _discard ───────────────────────────

def test_metadata_reads_key_value_pairs(tmp_path):
    db = tmp_path / "x.sqlite3"
    _write_db(db, "off_metadata", {"dataset": "off-2026-07-17", "products": 100})

    assert rm._metadata(db, "off_metadata") == {"dataset": "off-2026-07-17", "products": "100"}


def test_metadata_returns_none_for_a_missing_database(tmp_path):
    assert rm._metadata(tmp_path / "absent.sqlite3", "off_metadata") is None


def test_backup_restore_and_discard_round_trip(tmp_path):
    path = tmp_path / "x.sqlite3"
    path.write_text("original")

    backup = rm._backup(path)
    path.write_text("corrupted")
    rm._restore(backup, path)

    assert path.read_text() == "original"

    rm._discard(backup)
    assert not backup.exists()


def test_backup_of_a_missing_file_returns_none(tmp_path):
    assert rm._backup(tmp_path / "absent.sqlite3") is None


# ── refresh_one ──────────────────────────────────────────────────────────

def test_refresh_one_no_ops_when_the_dataset_is_unchanged(mirror, monkeypatch):
    """--auto-update decided there was nothing newer -- refresh_one must
    detect that and publish/restart nothing, given the already-installed
    dataset already has a published release (the ordinary steady-state
    case -- see the self-heal test below for when it doesn't)."""
    fake = FakeRuns(mirror, new_metadata=None, gh_tag_exists=True)
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is False
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake.calls)
    assert not any(c[:3] == ["gh", "release", "upload"] for c in fake.calls)


def test_refresh_one_self_heals_a_missing_release_for_an_unchanged_dataset(
        mirror, monkeypatch):
    """The gap this session actually hit: a dataset already installed (no
    rebuild needed) but never published as a release -- e.g. a previous
    manual rebuild, or a prior run that crashed between building and
    publishing. --auto-update has no reason to rebuild something already
    installed, so nothing else would ever catch this."""
    fake = FakeRuns(mirror, new_metadata=None, gh_tag_exists=False)
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is False    # the running service has this data already
    assert any(c[:3] == ["gh", "release", "create"] for c in fake.calls)


def test_dry_run_previews_the_self_heal_without_publishing(mirror, monkeypatch):
    fake = FakeRuns(mirror, new_metadata=None, gh_tag_exists=False)
    monkeypatch.setattr(subprocess, "run", fake)

    rm.refresh_one(mirror, dry_run=True)

    assert any(c[:3] == ["gh", "release", "view"] for c in fake.calls)
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake.calls)
    assert not any(c[:3] == ["gh", "release", "upload"] for c in fake.calls)


def test_refresh_one_detects_a_real_rebuild_and_publishes(mirror, monkeypatch):
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 105})
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is True
    gh_calls = [c for c in fake.calls if c[0] == "gh"]
    assert any(c[:3] == ["gh", "release", "view"] for c in gh_calls)
    assert any(c[:3] == ["gh", "release", "create"] for c in gh_calls)


def test_refresh_one_uploads_instead_of_creating_when_the_tag_already_exists(
        mirror, monkeypatch):
    fake = FakeRuns(
        mirror, new_metadata={"dataset": "off-2026-07-18", "products": 105},
        gh_tag_exists=True,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    rm.refresh_one(mirror, dry_run=False)

    gh_calls = [c for c in fake.calls if c[0] == "gh"]
    assert any(c[:3] == ["gh", "release", "upload"] for c in gh_calls)
    assert not any(c[:3] == ["gh", "release", "create"] for c in gh_calls)


def test_refresh_one_aborts_and_restores_on_a_row_count_shrink(mirror, monkeypatch):
    """A rebuild reporting far fewer rows than before is treated as a bad
    upstream export -- restored from backup, never published."""
    original_bytes = mirror.db_path.read_bytes()
    # 100 -> 50 is a 50% shrink, well past the 10% abort threshold.
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 50})
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is False
    assert not any(c[0] == "gh" for c in fake.calls)
    # The pre-rebuild database was restored, not left as the shrunk one.
    assert mirror.db_path.read_bytes() == original_bytes
    assert rm._metadata(mirror.db_path, mirror.metadata_table)["dataset"] == "off-2026-07-17"


def test_refresh_one_tolerates_a_shrink_within_the_threshold(mirror, monkeypatch):
    """100 -> 95 is only a 5% shrink -- within normal day-over-day variance,
    not an abort."""
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 95})
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is True


def test_refresh_one_leaves_the_database_untouched_when_the_build_fails(
        mirror, monkeypatch):
    original_bytes = mirror.db_path.read_bytes()
    fake = FakeRuns(mirror, build_fails=True)
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=False)

    assert rebuilt is False
    assert not any(c[0] == "gh" for c in fake.calls)
    assert mirror.db_path.read_bytes() == original_bytes


def test_dry_run_skips_publishing(mirror, monkeypatch):
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 105})
    monkeypatch.setattr(subprocess, "run", fake)

    rebuilt = rm.refresh_one(mirror, dry_run=True)

    assert rebuilt is True
    assert not any(c[0] == "gh" for c in fake.calls)


# ── release body / title ─────────────────────────────────────────────────

@pytest.fixture
def fdc_mirror(tmp_path):
    db_path = tmp_path / "fdc.sqlite3"
    archive_path = tmp_path / "fdc.sqlite3.xz"
    _write_db(db_path, "fdc_metadata", {
        "dataset": "FoodData_Central_branded_food_csv_2026-04-30", "barcodes": 100})
    archive_path.write_bytes(b"fake archive")
    return rm.Mirror(
        name="USDA FDC",
        build_script=Path("build_fdc_db.py"),
        db_path=db_path,
        archive_path=archive_path,
        metadata_table="fdc_metadata",
        count_key="barcodes",
        release_title_prefix="FDC branded foods —",
        release_body=rm._fdc_release_body,
        tag_for_dataset=rm._fdc_tag_for_dataset,
    )


# ── FDC's release tag differs from its raw dataset string ──────────────

def test_fdc_tag_for_dataset_extracts_the_date_with_the_fdc_prefix():
    assert rm._fdc_tag_for_dataset(
        "FoodData_Central_branded_food_csv_2026-04-30") == "fdc-2026-04-30"


def test_off_tag_for_dataset_is_the_dataset_itself():
    """OFF's dataset metadata value is already its own release tag -- no
    transformation, unlike FDC."""
    assert rm._identity_tag("off-2026-07-18") == "off-2026-07-18"


def test_publish_release_uses_the_fdc_release_tag_not_the_raw_dataset_string(
        fdc_mirror, monkeypatch):
    """The bug this test guards against: publishing (or checking for) a
    release under the raw fdc_metadata dataset string instead of the
    "fdc-<date>" tag build_fdc_db.py's own download_release() actually
    looks for -- would silently create releases nothing could ever find."""
    fake = FakeRuns(fdc_mirror, new_metadata={
        "dataset": "FoodData_Central_branded_food_csv_2026-10-15", "barcodes": 450000})
    monkeypatch.setattr(subprocess, "run", fake)

    rm.refresh_one(fdc_mirror, dry_run=False)

    gh_calls = [c for c in fake.calls if c[0] == "gh"]
    assert any("fdc-2026-10-15" in c for c in gh_calls)
    assert not any("FoodData_Central_branded_food_csv_2026-10-15" in c for c in gh_calls)


def test_off_release_body_reports_the_real_product_count():
    meta = {"dataset": "off-2026-07-18", "products": "2241619", "schema_version": "3"}
    body = rm._off_release_body(meta)
    assert "2,241,619" in body


def test_fdc_release_body_reports_the_real_barcode_count():
    meta = {"dataset": "FoodData_Central_branded_food_csv_2026-04-30",
            "barcodes": "442095", "schema_version": "2"}
    body = rm._fdc_release_body(meta)
    assert "442,095" in body


# ── main(): restart behaviour ─────────────────────────────────────────────

def test_main_restarts_once_when_something_rebuilt(mirror, monkeypatch, tmp_path):
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 105})
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(rm, "MIRRORS", {"off": mirror})
    monkeypatch.setattr(sys, "argv", ["refresh_mirrors.py"])

    rm.main()

    restart_calls = [c for c in fake.calls if "systemctl" in c]
    assert len(restart_calls) == 1
    assert restart_calls[0][-2:] == ["restart", "nutrition-api.service"]


def test_main_does_not_restart_when_nothing_rebuilt(mirror, monkeypatch):
    fake = FakeRuns(mirror, new_metadata=None)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(rm, "MIRRORS", {"off": mirror})
    monkeypatch.setattr(sys, "argv", ["refresh_mirrors.py"])

    rm.main()

    assert not any("systemctl" in c for c in fake.calls)


def test_main_no_restart_flag_skips_the_restart_even_after_a_rebuild(
        mirror, monkeypatch):
    fake = FakeRuns(mirror, new_metadata={"dataset": "off-2026-07-18", "products": 105})
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(rm, "MIRRORS", {"off": mirror})
    monkeypatch.setattr(sys, "argv", ["refresh_mirrors.py", "--no-restart"])

    rm.main()

    assert not any("systemctl" in c for c in fake.calls)
