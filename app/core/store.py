"""
On-disk response store: one JSON file per upstream response.

Two jobs, and the first is not the obvious one.

**It keeps us inside the upstream budgets.** The in-memory cache dies with the
process, so every deploy re-spends Open Food Facts' allowance — all fifteen
requests a minute of it — re-fetching barcodes we had already seen. It is also
per-worker, so two workers pay twice. A response on disk costs no request at
all, which is the difference between a service that can be public and one that
gets its IP banned.

**It accumulates a corpus.** Each file is a self-describing record of what an
upstream actually said and when, so `scripts/import_store_to_sqlite.py` can
build a queryable database from it later, and so a future session can work from
real payloads instead of re-hitting somebody else's API.

Design notes, most of them scars from elsewhere in this repo:

* **Writes are atomic.** A record is written to a temporary file beside its
  target and `os.replace()`d into place. Nothing ever reads a half-written file,
  and a crash mid-write leaves the previous record intact — the GPC importer
  learned this the hard way.
* **Timestamps are UTC, always**, ISO-8601 with an explicit offset. A naive
  local timestamp in an archive is worse than none: it is wrong somewhere, and
  it does not say where.
* **The payload is stored as it arrived.** Formatting is our business and it
  changes; the record is of what the upstream said, not of what we made of it.
* **A hit does not spend rate-limit budget**, because no request is made.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

STORE_DIR = Path(os.environ.get(
    "RESPONSE_STORE_DIR", str(REPO_ROOT / "data" / "responses"),
))
STORE_ENABLED = os.environ.get("RESPONSE_STORE_ENABLED", "1") not in ("0", "false", "")

# How long a stored response may be served before we re-fetch it. Food
# composition is close to static — a product's ingredients do not change weekly
# — so this is measured in days, unlike the in-memory cache's minutes.
STORE_TTL_DAYS = float(os.environ.get("RESPONSE_STORE_TTL_DAYS", "30"))

# The record format. Bump when the envelope changes, so an importer reading an
# older corpus knows what it is looking at rather than guessing.
SCHEMA_VERSION = 1

# Namespaces. Kept explicit rather than derived from a source name, because
# these become directory names and table names and want to be stable.
OFF_PRODUCT = "off/product"      # barcode        -> raw Open Food Facts payload
USDA_FOOD = "usda/food"          # fdc_id         -> USDA food record
USDA_UPC = "usda/upc"            # normalized GTIN -> the fdc_id it resolved to

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def utcnow() -> datetime:
    """Now, in UTC, with the offset attached."""
    return datetime.now(timezone.utc)


def _safe(key: str) -> str:
    """Make a key safe to be a filename.

    Keys are barcodes and numeric ids, but they arrive from the network, so a
    key containing "../" must not be able to write outside the store.
    """
    return _SAFE_KEY.sub("_", str(key))[:120]


def path_for(namespace: str, key: str) -> Path:
    """Where a record lives.

    Sharded two levels deep on the key: a flat directory of a million barcodes
    is painful for every tool that ever has to look at it.
    """
    safe = _safe(key)
    shard = (safe[:2] or "__", safe[2:4] or "__")
    return STORE_DIR / namespace / shard[0] / shard[1] / f"{safe}.json"


def _is_fresh(fetched_at: str, ttl_days: float) -> bool:
    try:
        when = datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:               # a naive timestamp is not trustworthy
        return False
    return utcnow() - when < timedelta(days=ttl_days)


def get(namespace: str, key: str, ttl_days: float | None = None) -> dict | None:
    """Return a stored payload, or None if it is absent, stale or unreadable.

    Never raises: a corrupt file in the cache must not be able to fail a
    request that would otherwise have succeeded.
    """
    if not STORE_ENABLED:
        return None

    ttl = STORE_TTL_DAYS if ttl_days is None else ttl_days
    path = path_for(namespace, key)
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Discarding unreadable store record %s: %s", path, e)
        return None

    if not _is_fresh(record.get("fetched_at", ""), ttl):
        return None
    return record.get("payload")


def put(namespace: str, key: str, payload) -> None:
    """Store a payload, atomically. Never raises.

    A store that can fail a request is worse than no store: this is an
    optimisation and an archive, and neither is worth a 500.
    """
    if not STORE_ENABLED:
        return

    record = {
        "schema_version": SCHEMA_VERSION,
        "namespace": namespace,
        "key": str(key),
        "fetched_at": utcnow().isoformat(),   # UTC, with the offset attached
        "payload": payload,
    }

    path = path_for(namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target, then swap it in: a reader sees the old
        # record or the new one, never a half-written file.
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as tmp:
            json.dump(record, tmp, ensure_ascii=False, indent=1, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("Could not store %s/%s: %s", namespace, key, e)


def iter_records(namespace: str | None = None):
    """Walk the corpus. Used by the SQLite importer, not by the request path."""
    root = STORE_DIR / namespace if namespace else STORE_DIR
    if not root.exists():
        return
    for path in sorted(root.rglob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping unreadable record %s: %s", path, e)
            continue
        record["_path"] = str(path)
        yield record


def stats() -> dict:
    """Record counts per namespace, for /health and for the importer's report."""
    counts = {}
    for namespace in (OFF_PRODUCT, USDA_FOOD, USDA_UPC):
        root = STORE_DIR / namespace
        counts[namespace] = sum(1 for _ in root.rglob("*.json")) if root.exists() else 0
    return {
        "enabled": STORE_ENABLED,
        "dir": str(STORE_DIR),
        "ttl_days": STORE_TTL_DAYS,
        "records": counts,
    }
