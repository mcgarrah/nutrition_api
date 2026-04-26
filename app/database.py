"""
Async SQLite database access for the Nutrition API.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gpc.sqlite3"

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return the shared database connection, creating it on first call."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
    return _db


async def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
