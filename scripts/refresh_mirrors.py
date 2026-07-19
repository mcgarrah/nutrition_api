#!/usr/bin/env python3
"""
Scheduled refresh of the local FDC/OFF bulk mirrors.

Before this, refreshing a mirror meant remembering to run `build_off_db.py`
/ `build_fdc_db.py --auto-update`, then `gh release create`/`upload` the
new archive by hand, then restart the service -- a fully manual loop that
this session repeated by hand every time fresher data was actually needed
(most recently 2026-07-18, to pick up the OFF outlier-storage fix -- and
that rebuild was never published as a release, exactly the kind of gap
this script exists to close). See PLAN.md item 3.

For each mirror, in order:
  1. Record the currently-installed dataset and row count (before touching
     anything -- this is the baseline the rebuild is checked against, and
     the safety net a bad rebuild gets restored from).
  2. Run `build_X_db.py --auto-update` as a subprocess (not an in-process
     import -- the same isolation app/main.py's own startup lifespan
     already relies on for import_gpc_xml.py, so one script's argparse
     state/globals can never leak into another's).
  3. If the installed dataset didn't change, there was nothing to do --
     `--auto-update` already no-ops correctly on its own, this is just
     detecting that so the steps below don't run for nothing.
  4. If it changed, check the new row count against the baseline. A
     shrink past ROW_COUNT_SHRINK_ABORT_FRACTION (upstream export
     glitches happen) restores the pre-rebuild database and archive from
     the backup taken in step 1 and stops there for that mirror --
     "a >10% shrink aborts" from the original plan sketch, made real:
     the bad build never reaches production, not merely logged after the
     fact.
  5. A build that passes the row-count check gets published as a GitHub
     release asset (`gh release create`, or `gh release upload --clobber`
     if the tag already exists) -- the step the by-hand loop always
     skipped, since build_*.py's own download_release() only ever *reads*
     a published release, never publishes one.

If anything actually rebuilt (and wasn't aborted), `nutrition-api.service`
is restarted once at the end so the running process picks up the new
database -- not once per mirror.

Needs `gh` authenticated as whatever user runs this (same requirement the
by-hand loop already had), and passwordless sudo for exactly
`systemctl restart nutrition-api.service` if run unattended from the
systemd timer -- see deploy/README.md for the one-time sudoers setup;
this script does not modify system policy itself.

Usage:
    python scripts/refresh_mirrors.py                 # refresh both
    python scripts/refresh_mirrors.py --off-only
    python scripts/refresh_mirrors.py --fdc-only
    python scripts/refresh_mirrors.py --dry-run        # build+check, skip publish/restart
    python scripts/refresh_mirrors.py --no-restart

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# A rebuild whose row count falls below this fraction of the previous
# build's is treated as a bad upstream export, not real data -- restored
# from backup rather than shipped. Not the serving TTL, not a version
# check: purely "does this look like a plausible day-over-day change."
ROW_COUNT_SHRINK_ABORT_FRACTION = 0.10


def _identity_tag(dataset: str) -> str:
    return dataset


class Mirror:
    def __init__(self, name, build_script, db_path, archive_path,
                 metadata_table, count_key, release_title_prefix, release_body,
                 tag_for_dataset=_identity_tag):
        self.name = name
        self.build_script = build_script
        self.db_path = db_path
        self.archive_path = archive_path
        self.metadata_table = metadata_table
        self.count_key = count_key
        self.release_title_prefix = release_title_prefix
        self.release_body = release_body
        # The release *tag* is not always the raw `dataset` metadata value.
        # OFF's is ("off-2026-07-18" either way) but FDC's dataset string is
        # "FoodData_Central_branded_food_csv_2026-04-30" while its actual
        # release tag (build_fdc_db.py's own release_url()) is "fdc-2026-04-30"
        # -- publishing/checking against the raw dataset string for FDC would
        # silently create releases under the wrong tag, never matching what
        # build_fdc_db.py's download_release() ever looks for.
        self.tag_for_dataset = tag_for_dataset


def _metadata(db_path: Path, table: str) -> dict | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return dict(conn.execute(f"SELECT key, value FROM {table}").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("Could not read metadata from %s: %s", db_path, e)
        return None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".previous")
    shutil.copy2(path, backup)
    return backup


def _restore(backup: Path | None, dest: Path) -> None:
    if backup is None:
        return
    shutil.copy2(backup, dest)


def _discard(backup: Path | None) -> None:
    if backup is not None:
        backup.unlink(missing_ok=True)


def _release_exists(tag: str) -> bool:
    return subprocess.run(
        ["gh", "release", "view", tag], cwd=REPO_ROOT,
        capture_output=True, text=True,
    ).returncode == 0


def publish_release(mirror: Mirror, dataset: str, dry_run: bool) -> None:
    tag = mirror.tag_for_dataset(dataset)

    if dry_run:
        logger.info("[dry run] Would publish %s as a release asset for tag %s.",
                    mirror.archive_path.name, tag)
        return

    meta = _metadata(mirror.db_path, mirror.metadata_table) or {}
    body = mirror.release_body(meta)
    title = f"{mirror.release_title_prefix} {tag.split('-', 1)[1]}"

    if _release_exists(tag):
        logger.info("Release %s already exists; uploading the new archive.", tag)
        subprocess.run(
            ["gh", "release", "upload", tag, str(mirror.archive_path), "--clobber"],
            cwd=REPO_ROOT, check=True,
        )
    else:
        logger.info("Publishing new release %s.", tag)
        subprocess.run(
            ["gh", "release", "create", tag, str(mirror.archive_path),
             "--title", title, "--notes", body],
            cwd=REPO_ROOT, check=True,
        )


def refresh_one(mirror: Mirror, dry_run: bool) -> bool:
    """Returns True if this mirror was actually rebuilt (and not aborted)."""
    logger.info("── %s ──", mirror.name)
    before = _metadata(mirror.db_path, mirror.metadata_table)
    before_dataset = before.get("dataset") if before else None
    before_count = int(before[mirror.count_key]) if before and mirror.count_key in before else None

    db_backup = _backup(mirror.db_path)
    archive_backup = _backup(mirror.archive_path)

    try:
        subprocess.run(
            [sys.executable, str(mirror.build_script), "--auto-update"],
            cwd=REPO_ROOT, check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error("%s: build script failed (exit %d). Leaving the existing "
                     "database untouched.", mirror.name, e.returncode)
        _discard(db_backup)
        _discard(archive_backup)
        return False

    after = _metadata(mirror.db_path, mirror.metadata_table)
    after_dataset = after.get("dataset") if after else None

    if after_dataset == before_dataset:
        _discard(db_backup)
        _discard(archive_backup)
        if after_dataset and not _release_exists(mirror.tag_for_dataset(after_dataset)):
            # A previous rebuild (by hand, or a prior run that crashed
            # between building and publishing) left the currently-installed
            # dataset without a release asset. --auto-update itself has no
            # reason to rebuild something already installed, so nothing
            # else would ever catch this -- self-heal it here instead of
            # leaving it silently missing until the next real rebuild.
            logger.warning(
                "%s: %s has no published release -- publishing it now "
                "(not from a fresh rebuild).", mirror.name, after_dataset)
            publish_release(mirror, after_dataset, dry_run)
        else:
            logger.info("%s: already current (%s); nothing to publish.",
                        mirror.name, after_dataset or "no local copy")
        return False

    after_count = int(after[mirror.count_key]) if after and mirror.count_key in after else None
    shrink_floor = before_count * (1 - ROW_COUNT_SHRINK_ABORT_FRACTION) if before_count else None
    if shrink_floor and after_count and after_count < shrink_floor:
        pct = (1 - after_count / before_count) * 100
        logger.error(
            "%s: row count shrank %.0f%% (%s -> %s) rebuilding %s -> %s. "
            "Aborting -- restoring the previous database.",
            mirror.name, pct, f"{before_count:,}", f"{after_count:,}",
            before_dataset, after_dataset,
        )
        _restore(db_backup, mirror.db_path)
        _restore(archive_backup, mirror.archive_path)
        _discard(db_backup)
        _discard(archive_backup)
        return False

    logger.info("%s: rebuilt %s -> %s (%s -> %s %s).",
                mirror.name, before_dataset or "nothing", after_dataset,
                f"{before_count:,}" if before_count else "?",
                f"{after_count:,}" if after_count else "?", mirror.count_key)
    _discard(db_backup)
    _discard(archive_backup)

    publish_release(mirror, after_dataset, dry_run)
    return True


def _off_release_body(meta: dict) -> str:
    return (
        "Automated rebuild via scripts/refresh_mirrors.py (PLAN.md item 3).\n\n"
        f"- source: `{meta.get('source_modified', meta.get('dataset', '?'))}`\n"
        f"- products: **{int(meta.get('products', 0)):,}**\n"
        f"- schema_version: {meta.get('schema_version', '?')}\n\n"
        "Install: `python scripts/build_off_db.py --auto-update`"
    )


def _fdc_release_body(meta: dict) -> str:
    return (
        "Automated rebuild via scripts/refresh_mirrors.py (PLAN.md item 3).\n\n"
        f"- source: `{meta.get('dataset', '?')}`\n"
        f"- barcodes: **{int(meta.get('barcodes', 0)):,}**\n"
        f"- schema_version: {meta.get('schema_version', '?')}\n\n"
        "Install: `python scripts/build_fdc_db.py --auto-update`"
    )


def _fdc_tag_for_dataset(dataset: str) -> str:
    """Mirrors build_fdc_db.py's own release_url(): the release tag is
    "fdc-<date>", not the raw "FoodData_Central_branded_food_csv_<date>"
    dataset string stored in fdc_metadata."""
    date = dataset.rsplit("_", 1)[-1]
    return f"fdc-{date}"


MIRRORS = {
    "off": Mirror(
        name="Open Food Facts",
        build_script=REPO_ROOT / "scripts" / "build_off_db.py",
        db_path=DATA_DIR / "off.sqlite3",
        archive_path=DATA_DIR / "off.sqlite3.xz",
        metadata_table="off_metadata",
        count_key="products",
        release_title_prefix="OFF products —",
        release_body=_off_release_body,
        # OFF's dataset metadata value ("off-2026-07-18") already is its own
        # release tag -- default identity tag_for_dataset is correct as-is.
    ),
    "fdc": Mirror(
        name="USDA FDC",
        build_script=REPO_ROOT / "scripts" / "build_fdc_db.py",
        db_path=DATA_DIR / "fdc.sqlite3",
        archive_path=DATA_DIR / "fdc.sqlite3.xz",
        metadata_table="fdc_metadata",
        count_key="barcodes",
        release_title_prefix="FDC branded foods —",
        release_body=_fdc_release_body,
        tag_for_dataset=_fdc_tag_for_dataset,
    ),
}


def restart_service(dry_run: bool) -> None:
    if dry_run:
        logger.info("[dry run] Would restart nutrition-api.service.")
        return
    logger.info("Restarting nutrition-api.service...")
    subprocess.run(
        ["sudo", "systemctl", "restart", "nutrition-api.service"], check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--off-only", action="store_true")
    parser.add_argument("--fdc-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and check, but skip publishing a release and restarting")
    parser.add_argument("--no-restart", action="store_true",
                        help="rebuild/publish as normal, but never restart the service")
    args = parser.parse_args()

    mirrors = list(MIRRORS.values())
    if args.off_only:
        mirrors = [MIRRORS["off"]]
    elif args.fdc_only:
        mirrors = [MIRRORS["fdc"]]

    rebuilt = [refresh_one(m, args.dry_run) for m in mirrors]

    if any(rebuilt) and not args.no_restart:
        restart_service(args.dry_run)
    elif any(rebuilt):
        logger.info("Rebuilt, but --no-restart was given; the running service "
                    "still has the old database until its next restart.")
    else:
        logger.info("Nothing rebuilt; nothing to restart.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
