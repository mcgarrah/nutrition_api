#!/usr/bin/env python3
"""
Build the local USDA FDC branded-foods database from the bulk dataset.

FDC publishes its whole branded corpus as a zip of CSVs twice a year (April and
October). Importing it locally takes barcode lookups off the network entirely:
no API key, no rate limit, no fuzzy-search mismatch, and a lookup that resolves
in microseconds instead of a round trip.

Two things make this cheap enough to run on a small box:

  * Nothing is extracted. Each CSV is streamed straight out of the zip, so the
    3.1 GB of uncompressed data never exists on disk or in memory. (An earlier
    version extracted to /tmp, which on a container is a RAM disk — it took the
    whole machine down.) Peak RSS is a couple of hundred MB.

  * Nutrient rows arrive grouped by food, so they are pivoted in a single pass
    holding one food at a time.

Nutrient mapping goes through app.core.nutrients.from_usda — the same function
the live API uses — so the local copy and the upstream path cannot drift apart
about which id is which, or what unit it is in.

Deduplication. A GTIN is *not* unique in FDC: it republishes a product as a new
fdc_id every time the label changes, so 2.0M branded records collapse to just
442,095 distinct barcodes — an average of 4.5 revisions each, and up to 38. The
revisions disagree: 31% of colliding barcodes report different calories. Keeping
them all would mean the answer depended on which row we happened to read first.

The newest revision therefore defines the product. Where it is merely *silent*
about a nutrient, an earlier revision may fill the gap — FDC's revisions are
often partial, and a missing figure is a hole in the paperwork rather than a
claim that the food contains none of it.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import csv
import fcntl
import io
import logging
import os
import re
import resource
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.nutrients import NUTRIENTS, from_usda  # noqa: E402
from app.core.usda_fdc import normalize_gtin  # noqa: E402

logger = logging.getLogger("build_fdc_db")

FDC_DATASETS = "https://fdc.nal.usda.gov/fdc-datasets/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "fdc.sqlite3"
ARCHIVE_PATH = DATA_DIR / "fdc.sqlite3.xz"

NUTRIENT_FIELDS = [spec.field for spec in NUTRIENTS]
# 2: added foods_fts (name search moved off a leading-wildcard LIKE scan).
SCHEMA_VERSION = "2"
BATCH = 50_000

# The columns we take from each CSV. Everything else in the bulk dataset —
# food_attribute, the update log, 462 of the 477 nutrients — is skipped.
BRANDED_COLUMNS = (
    "fdc_id", "gtin_upc", "brand_owner", "brand_name", "ingredients",
    "serving_size", "serving_size_unit", "household_serving_fulltext",
    "branded_food_category",
)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _step(msg: str) -> None:
    logger.info("%s  (peak RSS %.0f MB)", msg, peak_rss_mb())


def _rows(zf: zipfile.ZipFile, base: str, name: str):
    """Stream one CSV member of the zip as rows, without extracting it."""
    handle = zf.open(base + name)
    return csv.reader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))


def _base_dir(zf: zipfile.ZipFile) -> str:
    """The single top-level directory the FDC zip wraps everything in."""
    for name in zf.namelist():
        head = name.split("/")[0]
        if head:
            return head + "/"
    raise ValueError("zip appears to be empty")


def _float_or_none(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _load_nutrient_units(zf, base) -> dict[int, tuple[str, str]]:
    """id -> (name, unit) from nutrient.csv.

    The unit matters: FDC carries vitamin D as both micrograms (1114) and
    International Units (1110), and from_usda needs the declared unit to tell
    them apart rather than publishing an IU number as micrograms.
    """
    reader = _rows(zf, base, "nutrient.csv")
    header = next(reader)
    i_id, i_name, i_unit = (header.index(c) for c in ("id", "name", "unit_name"))
    units: dict[int, tuple[str, str]] = {}
    for row in reader:
        try:
            units[int(row[i_id])] = (row[i_name], row[i_unit])
        except (ValueError, IndexError):
            continue
    return units


def _load_foods(db, zf, base) -> int:
    """food.csv -> description and publication date."""
    db.execute("CREATE TABLE stg_food ("
               "fdc_id INTEGER PRIMARY KEY, description TEXT, published TEXT)")
    reader = _rows(zf, base, "food.csv")
    header = next(reader)
    i_fdc, i_desc, i_pub = (
        header.index(c) for c in ("fdc_id", "description", "publication_date")
    )
    batch, total = [], 0
    for row in reader:
        try:
            batch.append((int(row[i_fdc]), row[i_desc] or None, row[i_pub] or None))
        except (ValueError, IndexError):
            continue
        if len(batch) >= BATCH:
            db.executemany("INSERT OR IGNORE INTO stg_food VALUES (?,?,?)", batch)
            total += len(batch)
            batch.clear()
    db.executemany("INSERT OR IGNORE INTO stg_food VALUES (?,?,?)", batch)
    return total + len(batch)


def _load_branded(db, zf, base) -> tuple[int, int]:
    """branded_food.csv -> barcode and product identity."""
    db.execute("""CREATE TABLE stg_branded (
        fdc_id INTEGER PRIMARY KEY, gtin14 TEXT, brand_owner TEXT,
        brand_name TEXT, ingredients TEXT, serving_size REAL,
        serving_size_unit TEXT, household_serving TEXT, category TEXT)""")
    reader = _rows(zf, base, "branded_food.csv")
    header = next(reader)
    col = {name: header.index(name) for name in BRANDED_COLUMNS}

    batch, total, rejected = [], 0, 0
    for row in reader:
        try:
            fdc_id = int(row[col["fdc_id"]])
        except (ValueError, IndexError):
            continue
        # Normalised to GTIN-14 with the same function the live lookup uses, so
        # a barcode that matches upstream matches here too.
        gtin14 = normalize_gtin(row[col["gtin_upc"]])
        if not gtin14:
            rejected += 1
            continue
        batch.append((
            fdc_id, gtin14,
            row[col["brand_owner"]] or None,
            row[col["brand_name"]] or None,
            row[col["ingredients"]] or None,
            _float_or_none(row[col["serving_size"]]),
            row[col["serving_size_unit"]] or None,
            row[col["household_serving_fulltext"]] or None,
            row[col["branded_food_category"]] or None,
        ))
        if len(batch) >= BATCH:
            db.executemany(
                "INSERT OR IGNORE INTO stg_branded VALUES (?,?,?,?,?,?,?,?,?)", batch)
            total += len(batch)
            batch.clear()
    db.executemany("INSERT OR IGNORE INTO stg_branded VALUES (?,?,?,?,?,?,?,?,?)", batch)
    return total + len(batch), rejected


def _load_nutrients(db, zf, base, units) -> tuple[int, int]:
    """food_nutrient.csv -> one pivoted row per food.

    26M rows, streamed. They arrive grouped by fdc_id (verified: zero foods have
    their rows split across the file), so one food's entries are accumulated and
    handed to from_usda as a batch, then flushed. Memory stays flat.
    """
    columns = ", ".join(f"{field} REAL" for field in NUTRIENT_FIELDS)
    db.execute(f"CREATE TABLE stg_nutrients (fdc_id INTEGER PRIMARY KEY, {columns})")
    placeholders = ",".join("?" * (len(NUTRIENT_FIELDS) + 1))
    insert = f"INSERT OR IGNORE INTO stg_nutrients VALUES ({placeholders})"

    reader = _rows(zf, base, "food_nutrient.csv")
    header = next(reader)
    i_fdc, i_nut, i_amt = (
        header.index(c) for c in ("fdc_id", "nutrient_id", "amount")
    )

    batch: list[tuple] = []
    state = {"rows": 0, "foods": 0, "current": None, "entries": []}

    def flush() -> None:
        if state["current"] is None or not state["entries"]:
            return
        # The same mapper the live API uses: selects by id, checks the declared
        # unit, converts kJ to kcal and vitamin D from IU.
        values = from_usda(state["entries"])
        if not values:
            return
        batch.append((state["current"],
                      *(values.get(field) for field in NUTRIENT_FIELDS)))
        if len(batch) >= BATCH:
            db.executemany(insert, batch)
            state["foods"] += len(batch)
            batch.clear()

    for row in reader:
        state["rows"] += 1
        try:
            fdc_id = int(row[i_fdc])
            nutrient_id = int(row[i_nut])
        except (ValueError, IndexError):
            continue
        if fdc_id != state["current"]:
            flush()
            state["current"], state["entries"] = fdc_id, []
        amount = _float_or_none(row[i_amt])
        if amount is None:
            continue
        name, unit = units.get(nutrient_id, (None, None))
        state["entries"].append(
            {"id": nutrient_id, "name": name, "amount": amount, "unit": unit})

    flush()
    db.executemany(insert, batch)
    state["foods"] += len(batch)
    return state["rows"], state["foods"]


IDENTITY_FIELDS = (
    "fdc_id", "published", "description", "brand_owner", "brand_name",
    "ingredients", "serving_size", "serving_size_unit", "household_serving",
    "category",
)


def _build_served_table(db) -> tuple[int, int]:
    """Collapse the staging tables to one row per barcode.

    A GTIN maps to as many as 38 fdc_ids — FDC republishes a product as a new
    record whenever its label changes — and 31% of colliding barcodes disagree on
    calories. Without a rule the answer would depend on which revision we
    happened to read first.

    The rule: the newest revision defines the product (its name, brand,
    ingredients, and every nutrient it declares). Where the newest revision is
    simply *silent* about a nutrient, we fall back to the most recent earlier
    revision that did declare one, rather than reporting it as unknown — FDC's
    revisions are frequently partial, and a missing figure is a gap in the
    paperwork rather than a claim that the food contains none.

    Rows arrive ordered by barcode then recency, so each barcode's revisions are
    contiguous and one product is held at a time.
    """
    declared = ", ".join(f"{field} REAL" for field in NUTRIENT_FIELDS)
    # WITHOUT ROWID: the barcode *is* the key, so the table is stored in barcode
    # order and a lookup needs no second index.
    db.execute(f"""CREATE TABLE foods (
        gtin14 TEXT PRIMARY KEY, fdc_id INTEGER NOT NULL, published TEXT,
        description TEXT, brand_owner TEXT, brand_name TEXT, ingredients TEXT,
        serving_size REAL, serving_size_unit TEXT, household_serving TEXT,
        category TEXT, {declared}) WITHOUT ROWID""")

    columns = ["gtin14", *IDENTITY_FIELDS, *NUTRIENT_FIELDS]
    placeholders = ",".join("?" * len(columns))
    insert = (f"INSERT INTO foods ({', '.join(columns)}) "
              f"VALUES ({placeholders})")

    reader = db.cursor()
    reader.execute(f"""
        SELECT b.gtin14, b.fdc_id, f.published, f.description, b.brand_owner,
               b.brand_name, b.ingredients, b.serving_size, b.serving_size_unit,
               b.household_serving, b.category,
               {", ".join(f"n.{field}" for field in NUTRIENT_FIELDS)}
        FROM stg_branded b
        LEFT JOIN stg_food f USING (fdc_id)
        LEFT JOIN stg_nutrients n USING (fdc_id)
        ORDER BY b.gtin14, f.published DESC, b.fdc_id DESC""")

    writer = db.cursor()
    n_identity = 1 + len(IDENTITY_FIELDS)
    batch: list[tuple] = []
    served = filled = 0
    current: str | None = None
    identity: tuple = ()
    values: list[float | None] = []

    def flush() -> None:
        nonlocal served
        if current is None:
            return
        batch.append((current, *identity, *values))
        if len(batch) >= BATCH:
            writer.executemany(insert, batch)
            served += len(batch)
            batch.clear()

    for row in reader:
        gtin14 = row[0]
        if gtin14 != current:
            flush()
            # The newest revision: it defines the product.
            current = gtin14
            identity = row[1:n_identity]
            values = list(row[n_identity:])
            continue
        # An older revision of the same barcode. It may only fill silences.
        for i, value in enumerate(row[n_identity:]):
            if values[i] is None and value is not None:
                values[i] = value
                filled += 1

    flush()
    writer.executemany(insert, batch)
    served += len(batch)
    return served, filled


def _build_fts_table(db) -> None:
    """An FTS5 index over description, for GET /api/v1/search.

    A standalone table, not an FTS5 "external content" table: `foods` is
    WITHOUT ROWID with a TEXT primary key, and external-content FTS5 needs an
    INTEGER rowid to link back to. Duplicating the (short) description text
    into the index is cheap next to the win — a leading-wildcard `LIKE '%q%'`
    is a full-table scan by construction; FTS5 lets a query use the index.
    `gtin14` is UNINDEXED: carried through for the join back to `foods`, not
    searched itself. `unicode61 remove_diacritics 2` casefolds and strips
    accents, so a search does not need to match them exactly.
    """
    db.execute("""CREATE VIRTUAL TABLE foods_fts USING fts5(
        gtin14 UNINDEXED, description,
        tokenize = 'unicode61 remove_diacritics 2'
    )""")
    db.execute("""INSERT INTO foods_fts (gtin14, description)
        SELECT gtin14, description FROM foods WHERE description IS NOT NULL""")


def build(zip_path: Path, out_path: Path, dataset: str) -> dict:
    """Build the database at `out_path` from the bulk zip. Returns statistics."""
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

    with zipfile.ZipFile(zip_path) as zf:
        base = _base_dir(zf)
        units = _load_nutrient_units(zf, base)
        _step(f"nutrient.csv: {len(units)} definitions")

        foods = _load_foods(db, zf, base)
        db.commit()
        _step(f"food.csv: {foods:,} foods")

        branded, rejected = _load_branded(db, zf, base)
        db.commit()
        _step(f"branded_food.csv: {branded:,} with a usable GTIN, {rejected:,} without")

        rows, pivoted = _load_nutrients(db, zf, base, units)
        db.commit()
        _step(f"food_nutrient.csv: {rows:,} rows -> {pivoted:,} foods")

    served, filled = _build_served_table(db)
    db.commit()
    _step(f"collapsed to {served:,} distinct barcodes "
          f"({branded - served:,} superseded revisions folded in, "
          f"{filled:,} nutrient gaps filled from earlier ones)")

    _build_fts_table(db)
    db.commit()
    _step("built foods_fts (name search index)")

    # The zip's own mtime is the release's publish time (download() stamps it
    # from Last-Modified), pinning the build to an exact upstream release.
    source_modified = datetime.fromtimestamp(
        zip_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    db.execute("CREATE TABLE fdc_metadata (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO fdc_metadata VALUES (?,?)", [
        ("dataset", dataset),
        ("source_url", FDC_DATASETS + dataset + ".zip"),
        ("source_modified", source_modified),
        ("import_timestamp",
         time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
        ("schema_version", SCHEMA_VERSION),
        ("barcodes", str(served)),
        # PLAN.md item 12: what fraction of upstream we actually kept, and
        # why -- previously only ever logged (the _step() lines above),
        # never queryable after the build finished. excluded = branded_
        # food.csv records with no usable GTIN (dropped outright); deduped
        # = superseded label revisions of a barcode already kept under its
        # newest revision (collapsed, not dropped).
        ("rows_read", str(branded + rejected)),
        ("excluded", str(rejected)),
        ("deduped", str(branded - served)),
    ])
    for table in ("stg_food", "stg_branded", "stg_nutrients"):
        db.execute(f"DROP TABLE {table}")
    db.commit()
    db.execute("VACUUM")
    db.commit()
    db.close()

    # Atomic: readers see either the old database or the new one, never a
    # half-built file. Same discipline as the GPC importer.
    os.replace(tmp_path, out_path)

    return {
        "barcodes": served,
        "superseded": branded - served,
        "nutrient_rows": rows,
        "size_mb": out_path.stat().st_size / 1e6,
        "seconds": time.time() - started,
        "peak_rss_mb": peak_rss_mb(),
    }


def compress(db_path: Path, archive_path: Path, preset: int = 9) -> dict:
    """Write an xz archive of the database beside it.

    The archive is what gets kept and distributed: xz -9 takes the database down
    by about 90%, small enough to attach to a release, while the database it
    expands to is far too big to keep in git.
    """
    import lzma
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


# Where we publish the built archive. The database itself is far too large for
# git, and committing the 27 MB archive would grow every clone by that much again
# at each refresh, forever — so it lives on a release instead.
RELEASE_REPO = os.environ.get("FDC_RELEASE_REPO", "mcgarrah/nutrition_api")
RELEASE_ASSET = "fdc.sqlite3.xz"

DOWNLOADS_PAGE = "https://fdc.nal.usda.gov/download-datasets.html"
_DATASET_RE = re.compile(r"FoodData_Central_branded_food_csv_(\d{4}-\d{2}-\d{2})")


def latest_dataset(timeout: float = 30.0) -> str | None:
    """The newest branded dataset FDC currently offers.

    Scraped from the downloads page, because the release day moves (2025-04-24,
    2025-12-18, 2026-04-30) and the directory listing itself returns 403. Dates
    are ISO, so the newest sorts last.
    """
    try:
        with urllib.request.urlopen(DOWNLOADS_PAGE, timeout=timeout) as response:
            page = response.read().decode("utf-8", "replace")
    except OSError as e:
        logger.warning("Could not reach the FDC downloads page: %s", e)
        return None
    dates = sorted(set(_DATASET_RE.findall(page)))
    if not dates:
        logger.warning("No branded datasets found on the downloads page.")
        return None
    return f"FoodData_Central_branded_food_csv_{dates[-1]}"


def installed_dataset(db_path: Path) -> str | None:
    """Which dataset the database on disk was built from, if there is one."""
    if not db_path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = db.execute(
                "SELECT value FROM fdc_metadata WHERE key = 'dataset'").fetchone()
        finally:
            db.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def release_url(dataset: str) -> str:
    """Where the prebuilt archive for a dataset lives."""
    date = dataset.rsplit("_", 1)[-1]
    return (f"https://github.com/{RELEASE_REPO}/releases/download/"
            f"fdc-{date}/{RELEASE_ASSET}")


def download_release(dataset: str, dest: Path) -> bool:
    """Fetch the prebuilt archive for a dataset, if one has been published.

    This is the cheap path: 27 MB and a few seconds, against 428 MB and seven
    minutes to rebuild the same thing from FDC's bulk CSVs. Returns False if no
    release exists yet — in which case the caller builds it, and presumably
    publishes it.
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
    # tempfile makes the file private (0600) and os.replace keeps that, which
    # leaves the archive unreadable to a service running as another user.
    os.chmod(dest, 0o644)
    logger.info("Downloaded %s (%.0f MB)", RELEASE_ASSET, dest.stat().st_size / 1e6)
    return True


@contextmanager
def build_lock(path: Path):
    """Serialize builds across processes.

    Two workers rebuilding at once would race on the same output file. The GPC
    importer learned this the hard way: both unlinked the database mid-read and
    the API started answering "disk I/O error".
    """
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


def download(dataset: str, dest: Path) -> Path:
    """Fetch the bulk zip, unless we already have it."""
    url = f"{FDC_DATASETS}{dataset}.zip"
    if dest.exists():
        logger.info("Using cached %s (%.0f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest
    logger.info("Downloading %s", url)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        with urllib.request.urlopen(url, timeout=60) as response:
            last_modified = response.headers.get("Last-Modified")
            while chunk := response.read(1 << 20):
                tmp.write(chunk)
        partial = Path(tmp.name)
    os.replace(partial, dest)
    os.chmod(dest, 0o644)
    # Stamp the file with the release's publish time, so `ls -l` reflects when
    # FDC built it rather than when we fetched it — as the OFF importer does.
    if last_modified:
        try:
            when = parsedate_to_datetime(last_modified).timestamp()
            os.utime(dest, (when, when))
        except (TypeError, ValueError):
            pass
    logger.info("Downloaded %.0f MB", dest.stat().st_size / 1e6)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dataset", default="FoodData_Central_branded_food_csv_2026-04-30",
        help="bulk dataset name, without the .zip (FDC refreshes in April and October)")
    parser.add_argument("--zip", type=Path,
                        help="use this local zip instead of downloading")
    parser.add_argument("--out", type=Path, default=DB_PATH)
    parser.add_argument("--archive", type=Path, default=ARCHIVE_PATH,
                        help="also write a compressed copy here")
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=Path("/var/tmp/fdc"),
                        help="where to keep the downloaded zip; must NOT be a "
                             "tmpfs such as /tmp, which is RAM")
    parser.add_argument("--auto-update", action="store_true",
                        help="rebuild only if FDC has published a newer dataset "
                             "(they refresh twice a year)")
    parser.add_argument("--check", action="store_true",
                        help="report what is installed and what is available, "
                             "then exit without changing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    have = installed_dataset(args.out)

    if args.check:
        available = latest_dataset()
        logger.info("installed: %s", have or "nothing")
        logger.info("available: %s", available or "could not reach FDC")
        if available and have != available:
            logger.info("A newer dataset is available. "
                        "Refresh with: %s --auto-update", Path(__file__).name)
        elif have:
            logger.info("Up to date.")
        return 0

    dataset = args.dataset
    if args.auto_update:
        available = latest_dataset()
        if available is None:
            # Unreachable is not a reason to throw away a working database.
            logger.warning("Keeping the existing copy (%s).", have or "none")
            return 0
        if available == have:
            logger.info("Already on the newest dataset (%s); nothing to do.", have)
            return 0
        logger.info("Newer dataset available: %s (installed: %s)",
                    available, have or "nothing")
        dataset = available

    with build_lock(args.work_dir / "build.lock"):
        # The cheap path: someone has already built this dataset and published
        # the archive. 27 MB and a few seconds, rather than 428 MB and seven
        # minutes to recompute the identical thing.
        if args.auto_update and not args.zip and download_release(dataset, args.archive):
            args.out.unlink(missing_ok=True)
            import app.core.fdc_local as fdc_local
            fdc_local.ARCHIVE_PATH = args.archive
            fdc_local.DB_PATH = args.out
            if fdc_local.ensure_database():
                logger.info("Installed %s from the published archive.", dataset)
                return 0
            logger.warning("The published archive would not expand; rebuilding.")

        zip_path = args.zip or download(dataset, args.work_dir / f"{dataset}.zip")

        stats = build(zip_path, args.out, dataset)
        logger.info(
            "Built %s: %s barcodes, %.0f MB, %.0fs, peak RSS %.0f MB",
            args.out.name, f"{stats['barcodes']:,}", stats["size_mb"],
            stats["seconds"], stats["peak_rss_mb"],
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
