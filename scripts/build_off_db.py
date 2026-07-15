#!/usr/bin/env python3
"""
Build the local Open Food Facts database from their daily CSV export.

Open Food Facts is the base layer of every lookup — name, brand, image,
ingredients, and provisional nutrition — and it is the slow half of a response:
a live product read is a few hundred milliseconds over the network, against a
few microseconds from a local copy. It is also the only source still spending a
rate-limit budget. OFF allows 15 product reads per minute per IP and enforces it
with a ban, so the fewer live reads we make, the safer the deployment.

They publish the whole corpus once a day as a single gzipped CSV (~1.3 GB). This
imports the usable subset — a product needs a barcode, a name, and at least one
nutrient we publish — which is a little under half of the 4.5M rows.

Same discipline as the FDC importer (scripts/build_fdc_db.py):

  * The CSV is streamed straight out of the gzip; the 9 GB of decompressed text
    never lands on disk or in memory. Peak RSS is ~200 MB.
  * Nutrient values are stored *raw*, exactly as OFF publishes them (grams, even
    for nutrients a label shows in mg). The conversion is app.core.nutrients
    .from_off, run at lookup time — the same function and the same call the live
    path makes — so the local copy and the API cannot disagree about units.

Unlike FDC's twice-yearly release, OFF rebuilds daily, and the export has no
dated filename — it is one rolling URL. The dataset is therefore identified by
its Last-Modified date, and that is what --auto-update compares.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import csv
import fcntl
import gzip
import io
import logging
import lzma
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.nutrients import NUTRIENTS, from_off  # noqa: E402
from app.core.usda_fdc import normalize_gtin  # noqa: E402

logger = logging.getLogger("build_off_db")

CSV_URL = ("https://static.openfoodfacts.org/data/"
           "en.openfoodfacts.org.products.csv.gz")
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "off.sqlite3"
ARCHIVE_PATH = DATA_DIR / "off.sqlite3.xz"

# Where the built archive is published, so --auto-update can fetch it (142 MB)
# instead of re-downloading and re-parsing the 1.3 GB export.
RELEASE_REPO = os.environ.get("OFF_RELEASE_REPO", "mcgarrah/nutrition_api")
RELEASE_ASSET = "off.sqlite3.xz"

NUTRIENT_FIELDS = [spec.field for spec in NUTRIENTS]
SCHEMA_VERSION = "1"
BATCH = 20_000

# The text columns we keep, mapped from OFF's CSV names. The *_tags columns are
# comma-separated lists that the live SDK returns already split, so they are
# stored joined and split again at lookup time.
TEXT_COLUMNS = {
    "product_name": "product_name",
    "brands": "brands",
    "image_url": "image_url",
    "ingredients_text": "ingredients_text",
    "quantity": "quantity",
    "serving_size": "serving_size",
    "categories": "categories_tags",
    "allergens": "allergens_tags",
    "labels": "labels_tags",
}
STORED_TEXT = list(TEXT_COLUMNS.keys())

# OFF products carry unbounded free text (ingredient lists, category chains); the
# default 128 KB field limit trips on the long ones.
csv.field_size_limit(16 * 1024 * 1024)


def peak_rss_mb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _step(msg: str) -> None:
    logger.info("%s  (peak RSS %.0f MB)", msg, peak_rss_mb())


def _open_csv(gz_path: Path):
    """Stream the gzipped CSV as tab-delimited rows, without decompressing to disk."""
    text = io.TextIOWrapper(gzip.open(gz_path, "rb"), encoding="utf-8", newline="")
    return csv.reader(text, delimiter="\t")


def _float_or_none(value: str) -> float | None:
    try:
        return float(value) if value not in ("", None) else None
    except ValueError:
        return None


def build(gz_path: Path, out_path: Path, dataset: str) -> dict:
    """Build the database at `out_path` from the gzipped export."""
    started = time.time()
    tmp_path = out_path.with_suffix(".building")
    if tmp_path.exists():
        tmp_path.unlink()

    db = sqlite3.connect(tmp_path)
    db.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous  = OFF;
        PRAGMA temp_store   = FILE;
        PRAGMA cache_size   = -64000;
    """)
    text_cols = ", ".join(f"{name} TEXT" for name in STORED_TEXT)
    nutrient_cols = ", ".join(f"{field} REAL" for field in NUTRIENT_FIELDS)
    # WITHOUT ROWID: the barcode is the key, so the row is looked up with no
    # secondary index.
    db.execute(f"""CREATE TABLE products (
        gtin14 TEXT PRIMARY KEY, modified INTEGER, {text_cols}, {nutrient_cols}
    ) WITHOUT ROWID""")

    all_cols = ["gtin14", "modified", *STORED_TEXT, *NUTRIENT_FIELDS]
    placeholders = ",".join("?" * len(all_cols))
    # OFF occasionally carries the same barcode twice; keep the newer row.
    updates = ", ".join(f"{c}=excluded.{c}"
                        for c in ["modified", *STORED_TEXT, *NUTRIENT_FIELDS])
    insert = (f"INSERT INTO products ({', '.join(all_cols)}) "
              f"VALUES ({placeholders}) "
              f"ON CONFLICT(gtin14) DO UPDATE SET {updates} "
              f"WHERE excluded.modified >= products.modified")

    reader = _open_csv(gz_path)
    header = next(reader)
    idx = {name: i for i, name in enumerate(header)}

    def cell(row, csv_name):
        i = idx.get(csv_name)
        return row[i] if i is not None and i < len(row) else ""

    batch: list[tuple] = []
    total = kept = 0
    for row in reader:
        total += 1
        gtin14 = normalize_gtin(cell(row, "code"))
        name = cell(row, "product_name").strip()
        if not gtin14 or not name:
            continue
        # Raw OFF values, keyed by our field names — exactly what the live
        # wrapper's _extract_nutrients produces. from_off (at lookup) converts
        # and drops the impossible; a product with nothing it can use is skipped.
        raw = {spec.field: cell(row, spec.off_key)
               for spec in NUTRIENTS if cell(row, spec.off_key)}
        if not from_off(raw):
            continue

        try:
            modified = int(cell(row, "last_modified_t") or 0)
        except ValueError:
            modified = 0

        batch.append((
            gtin14, modified,
            *(cell(row, TEXT_COLUMNS[name_]).strip() or None for name_ in STORED_TEXT),
            *(_float_or_none(raw.get(field, "")) for field in NUTRIENT_FIELDS),
        ))
        kept += 1
        if len(batch) >= BATCH:
            db.executemany(insert, batch)
            batch.clear()
        if total % 1_000_000 == 0:
            _step(f"{total:,} rows read, {kept:,} kept")

    db.executemany(insert, batch)
    db.commit()
    served = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    _step(f"{total:,} rows read, {served:,} usable products kept "
          f"({total - kept:,} skipped, {kept - served:,} deduped)")

    # The source file's own mtime is the export's publish time (download() stamps
    # it from Last-Modified), so recording it pins the build to an exact upstream
    # export — not just the day — for comparing runs.
    source_modified = datetime.fromtimestamp(
        gz_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    db.execute("CREATE TABLE off_metadata (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO off_metadata VALUES (?,?)", [
        ("dataset", dataset),
        ("source_url", CSV_URL),
        ("source_modified", source_modified),
        ("import_timestamp",
         time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
        ("schema_version", SCHEMA_VERSION),
        ("products", str(served)),
    ])
    db.commit()
    db.execute("VACUUM")
    db.commit()
    db.close()
    os.replace(tmp_path, out_path)

    return {
        "products": served,
        "rows_read": total,
        "size_mb": out_path.stat().st_size / 1e6,
        "seconds": time.time() - started,
        "peak_rss_mb": peak_rss_mb(),
    }


def compress(db_path: Path, archive_path: Path, preset: int = 9) -> dict:
    """Write an xz archive of the database beside it."""
    started = time.time()
    tmp = archive_path.with_suffix(".xz.partial")
    filters = [{"id": lzma.FILTER_LZMA2, "preset": preset | lzma.PRESET_EXTREME}]
    with open(db_path, "rb") as src, \
            lzma.open(tmp, "wb", format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64,
                      filters=filters) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    os.replace(tmp, archive_path)
    return {
        "size_mb": archive_path.stat().st_size / 1e6,
        "ratio": archive_path.stat().st_size / db_path.stat().st_size,
        "seconds": time.time() - started,
    }


def content_last_modified(timeout: float = 60.0) -> datetime | None:
    """When OFF last rebuilt the export, from its Last-Modified header.

    OFF has no dated filename — the export is one rolling URL rebuilt daily — so
    this header is the only version marker there is. It is the *content's* own
    timestamp, identical for everyone who fetches the same export, which is what
    makes it a stable name and a meaningful version rather than a download time.
    """
    request = urllib.request.Request(CSV_URL, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            header = response.headers.get("Last-Modified")
    except OSError as e:
        logger.warning("Could not reach the OFF export: %s", e)
        return None
    if not header:
        return None
    try:
        return parsedate_to_datetime(header).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def latest_dataset(timeout: float = 60.0) -> str | None:
    """The date of the export OFF is currently serving, as `off-YYYY-MM-DD`."""
    modified = content_last_modified(timeout)
    return "off-" + modified.strftime("%Y-%m-%d") if modified else None


def dated_download_name(modified: datetime | None) -> str:
    """The filename a download gets, stamped with the export's own timestamp.

    One file per export, named for the content rather than the moment we pulled
    it: re-running on the same day reuses the file, and each new daily export
    lands beside the last instead of overwriting it, so days can be compared.
    """
    when = modified or datetime.now(timezone.utc)
    return f"off-products-{when.strftime('%Y-%m-%dT%H%M%SZ')}.csv.gz"


def _install_download(partial: Path, dest: Path, modified: datetime | None) -> Path:
    """Move a finished download into place and stamp it with the content time."""
    os.replace(partial, dest)
    os.chmod(dest, 0o644)
    if modified is not None:
        # The file's own mtime carries the export's publish time, so `ls -l`
        # shows when OFF built it, not when we fetched it.
        os.utime(dest, (modified.timestamp(), modified.timestamp()))
    return dest


def installed_dataset(db_path: Path) -> str | None:
    """Which dataset the database on disk was built from, if there is one."""
    if not db_path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = db.execute(
                "SELECT value FROM off_metadata WHERE key = 'dataset'").fetchone()
        finally:
            db.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def release_url(dataset: str) -> str:
    return (f"https://github.com/{RELEASE_REPO}/releases/download/"
            f"{dataset}/{RELEASE_ASSET}")


def download_release(dataset: str, dest: Path) -> bool:
    """Fetch the prebuilt archive for a dataset, if one has been published.

    142 MB and a few seconds against 1.3 GB and twenty minutes to rebuild the
    same thing. Returns False if no release exists, in which case the caller
    builds it from the export.
    """
    url = release_url(dataset)
    logger.info("Looking for a prebuilt archive at %s", url)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
                while chunk := response.read(1 << 20):
                    tmp.write(chunk)
                partial = Path(tmp.name)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info("No release published for %s yet; building it.", dataset)
        else:
            logger.warning("Could not fetch the release archive: %s", e)
        return False
    except OSError as e:
        logger.warning("Could not fetch the release archive: %s", e)
        return False
    os.replace(partial, dest)
    os.chmod(dest, 0o644)
    logger.info("Downloaded %s (%.0f MB)", RELEASE_ASSET, dest.stat().st_size / 1e6)
    return True


def download(work_dir: Path) -> Path:
    """Fetch the gzipped export into a file named for the export's own date.

    Kept, not overwritten: every daily export accumulates under its own
    timestamped name so several days can be held side by side and diffed. Today's
    file is reused if it is already here.
    """
    modified = content_last_modified()
    dest = work_dir / dated_download_name(modified)
    if dest.exists():
        logger.info("Using cached %s (%.0f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest

    logger.info("Downloading %s -> %s", CSV_URL, dest.name)
    with tempfile.NamedTemporaryFile(dir=work_dir, delete=False) as tmp:
        with urllib.request.urlopen(CSV_URL, timeout=120) as response:
            while chunk := response.read(1 << 20):
                tmp.write(chunk)
        partial = Path(tmp.name)
    _install_download(partial, dest, modified)

    kept = sorted(work_dir.glob("off-products-*.csv.gz"))
    total_gb = sum(p.stat().st_size for p in kept) / 1e9
    logger.info("Downloaded %.0f MB (export dated %s). %d export(s) kept, %.1f GB total.",
                dest.stat().st_size / 1e6,
                modified.isoformat() if modified else "unknown", len(kept), total_gb)
    return dest


@contextmanager
def build_lock(path: Path):
    """Serialize builds across processes, as the GPC and FDC importers do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Another build holds the lock; waiting for it to finish.")
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--gz", type=Path,
                        help="use this local CSV.gz instead of downloading")
    parser.add_argument("--out", type=Path, default=DB_PATH)
    parser.add_argument("--archive", type=Path, default=ARCHIVE_PATH)
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=Path("/var/tmp/off"),
                        help="where to keep the download; must NOT be a tmpfs "
                             "such as /tmp, which is RAM")
    parser.add_argument("--auto-update", action="store_true",
                        help="rebuild only if OFF has published a newer export")
    parser.add_argument("--check", action="store_true",
                        help="report installed vs available, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    have = installed_dataset(args.out)

    if args.check:
        available = latest_dataset()
        logger.info("installed: %s", have or "nothing")
        logger.info("available: %s", available or "could not reach OFF")
        if available and have != available:
            logger.info("A newer export is available. Refresh with: %s --auto-update",
                        Path(__file__).name)
        elif have:
            logger.info("Up to date.")
        return 0

    dataset = latest_dataset() or time.strftime("off-%Y-%m-%d", time.gmtime())
    if args.auto_update:
        available = latest_dataset()
        if available is None:
            logger.warning("Keeping the existing copy (%s).", have or "none")
            return 0
        if available == have:
            logger.info("Already on the newest export (%s); nothing to do.", have)
            return 0
        logger.info("Newer export available: %s (installed: %s)", available, have or "nothing")
        dataset = available

    with build_lock(args.work_dir / "build.lock"):
        if args.auto_update and not args.gz and download_release(dataset, args.archive):
            args.out.unlink(missing_ok=True)
            import app.core.off_local as off_local
            off_local.ARCHIVE_PATH = args.archive
            off_local.DB_PATH = args.out
            if off_local.ensure_database():
                logger.info("Installed %s from the published archive.", dataset)
                return 0
            logger.warning("The published archive would not expand; rebuilding.")

        gz_path = args.gz or download(args.work_dir)

        stats = build(gz_path, args.out, dataset)
        logger.info(
            "Built %s: %s products from %s rows, %.0f MB, %.0fs, peak RSS %.0f MB",
            args.out.name, f"{stats['products']:,}", f"{stats['rows_read']:,}",
            stats["size_mb"], stats["seconds"], stats["peak_rss_mb"],
        )

        if not args.no_compress:
            archive = compress(args.out, args.archive)
            logger.info(
                "Compressed to %s: %.0f MB (%.0f%% of the database, %.0fs)",
                args.archive.name, archive["size_mb"], archive["ratio"] * 100,
                archive["seconds"],
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
