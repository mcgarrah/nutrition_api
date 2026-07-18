#!/usr/bin/env python3
"""
Delete response-store records nobody will ever be served again.

`app/core/store.py`'s `get()` already refuses to serve a record past
STORE_TTL_DAYS or with an untrustworthy timestamp -- but nothing ever
deleted the file itself, so the directory could only grow. This is the
periodic sweep that does: `store.prune()` removes anything past
STORE_PRUNE_AFTER_DAYS (a multiple of the serving TTL, not the TTL itself --
see the constant's own docstring for why) or unreadable outright.

Meant to run from a systemd timer (deploy/nutrition-api-prune.service +
.timer), the same "administrative sweep, separate from the request path"
split import_store_to_sqlite.py already uses for the corpus-export side of
this store. See PLAN.md item 8.

Usage:
    python scripts/prune_response_store.py               # prune for real
    python scripts/prune_response_store.py --dry-run      # report only
    python scripts/prune_response_store.py --older-than 60

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core import store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main():
    parser = argparse.ArgumentParser(
        description="Delete response-store records past their prune age",
    )
    parser.add_argument(
        "--older-than", type=float, default=None, metavar="DAYS",
        help=f"Prune age in days (default: {store.STORE_PRUNE_AFTER_DAYS:.0f}, "
             f"= {store.STORE_TTL_DAYS:.0f}-day serving TTL x 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be removed without deleting anything",
    )
    args = parser.parse_args()

    if not store.STORE_ENABLED:
        logging.info("Response store is disabled (RESPONSE_STORE_ENABLED=0) — nothing to prune.")
        return

    verb = "Would prune" if args.dry_run else "Pruning"
    logging.info("%s %s (older than %.0f days)...",
                 verb, store.STORE_DIR, args.older_than or store.STORE_PRUNE_AFTER_DAYS)

    result = store.prune(older_than_days=args.older_than, dry_run=args.dry_run)

    logging.info(
        "%s: scanned %d, removed %d (%s freed), %d error%s",
        "Dry run" if args.dry_run else "Done",
        result["scanned"], result["removed"], _fmt_bytes(result["bytes_freed"]),
        result["errors"], "" if result["errors"] == 1 else "s",
    )
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
