"""
Integration test against the real GS1 GPC XML shipped in the repository.

Every other GPC test runs on a hand-written fixture, which proves the importer
handles the XML *we* wrote. This one runs it against the actual 27 MB GS1
publication, which is the only thing that proves the parser matches the real
schema — element names, nesting, the segment codes, the D/M/YYYY dateUtc.

It takes a few seconds. That is the price of knowing the shipped data actually
imports, and it is the file the Docker build bakes in.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_gpc_xml as importer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_XML = REPO_ROOT / "data" / "imports" / "en-v20251127.xml"

pytestmark = pytest.mark.skipif(
    not BUNDLED_XML.exists(),
    reason="bundled GS1 XML not present in this checkout",
)


@pytest.fixture(scope="module")
def real_db(tmp_path_factory):
    """Import the real GS1 XML once, then assert against it."""
    db = tmp_path_factory.mktemp("real_gpc") / "gpc.sqlite3"
    counts = importer.import_food_gpc(str(BUNDLED_XML), db)
    return db, counts


def query(db, sql, params=()):
    conn = sqlite3.connect(db)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_the_shipped_xml_imports(real_db):
    db, counts = real_db
    assert counts["segments"] == 1          # Food/Beverage only
    assert counts["families"] > 20
    assert counts["classes"] > 100
    assert counts["bricks"] > 800


def test_only_the_food_segment_survives_the_filter(real_db):
    """The real file contains all 44 GPC segments — Arts/Crafts, Vehicles, etc.
    Exactly one must reach the database."""
    db, _ = real_db
    assert query(db, "SELECT segment_code FROM segments") == [("50000000",)]


def test_the_real_taxonomy_has_no_orphans(real_db):
    """Every child must resolve to a parent — a broken join here would surface
    as missing breadcrumbs in the API."""
    db, _ = real_db
    assert query(db, """
        SELECT COUNT(*) FROM families f
        LEFT JOIN segments s ON f.segment_code = s.segment_code
        WHERE s.segment_code IS NULL
    """)[0][0] == 0
    assert query(db, """
        SELECT COUNT(*) FROM classes c
        LEFT JOIN families f ON c.family_code = f.family_code
        WHERE f.family_code IS NULL
    """)[0][0] == 0
    assert query(db, """
        SELECT COUNT(*) FROM bricks b
        LEFT JOIN classes c ON b.class_code = c.class_code
        WHERE c.class_code IS NULL
    """)[0][0] == 0


def test_real_data_exercises_the_many_to_many_case(real_db):
    """The junction table earns its keep only if real GS1 data actually shares
    attribute types across bricks. Prove that it does."""
    db, _ = real_db
    shared = query(db, """
        SELECT att_type_code, COUNT(DISTINCT brick_code) AS bricks
        FROM brick_attribute_types
        GROUP BY att_type_code
        HAVING bricks > 1
        ORDER BY bricks DESC
        LIMIT 1
    """)
    assert shared, "no attribute type is shared across bricks — schema is pointless"
    assert shared[0][1] > 1


def test_attribute_types_are_deduplicated_in_the_real_import(real_db):
    """Occurrences far exceed unique rows — that gap is the data the old
    single-FK schema threw away."""
    db, counts = real_db
    unique = query(db, "SELECT COUNT(*) FROM attribute_types")[0][0]
    assert unique < counts["attribute_types"]
    assert query(db, "SELECT COUNT(*) FROM brick_attribute_types")[0][0] == \
        counts["brick_attribute_types"]


def test_version_comes_from_the_real_filename(real_db):
    db, _ = real_db
    meta = dict(query(db, "SELECT key, value FROM gpc_metadata"))
    assert meta["gpc_version"] == "20251127"


def test_the_real_dateutc_is_day_first(real_db):
    """The shipped file carries dateUtc="27/11/2025" — 27 November. Parsing it
    month-first fails outright, which is how the version silently became
    "unknown" and froze the auto-update."""
    db, _ = real_db
    meta = dict(query(db, "SELECT key, value FROM gpc_metadata"))
    assert meta["xml_date"] == "27/11/2025"

    # And the fallback path (no version in the filename) now resolves it
    assert importer.extract_version_from_path("cached.xml", "27/11/2025") == "20251127"


def test_known_food_bricks_are_present(real_db):
    """Spot-check the taxonomy actually contains recognisable food terms."""
    db, _ = real_db
    for term in ["Chocolate", "Milk", "Bread", "Coffee"]:
        rows = query(db, "SELECT 1 FROM bricks WHERE description LIKE ? LIMIT 1",
                     (f"%{term}%",))
        assert rows, f"no brick mentions {term!r}"


def test_every_brick_resolves_to_a_full_breadcrumb(real_db):
    """The lookup endpoint builds category_hierarchy from this join; if any
    brick can't reach its segment, the hierarchy comes back short."""
    db, _ = real_db
    incomplete = query(db, """
        SELECT COUNT(*) FROM bricks b
        JOIN classes c ON b.class_code = c.class_code
        JOIN families f ON c.family_code = f.family_code
        JOIN segments s ON f.segment_code = s.segment_code
        WHERE s.description IS NULL OR f.description IS NULL
           OR c.description IS NULL OR b.description IS NULL
    """)[0][0]
    assert incomplete == 0


def test_search_against_the_real_taxonomy_is_bounded(real_db, monkeypatch):
    """The case that motivated the cap: against the real 879-brick taxonomy a
    single-character query matched almost everything."""
    import app.database as database
    from fastapi.testclient import TestClient

    from app.main import app

    db, _ = real_db
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(database, "_db", None)
    client = TestClient(app)

    try:
        body = client.get("/api/v1/gpc/search/", params={"q": "e"}).json()

        # Hundreds of real matches, but the response is capped
        assert body["counts"]["bricks"] > 200
        assert len(body["bricks"]) <= 50
        assert body["truncated"] is True

        # A wildcard cannot opt out of the cap either
        wild = client.get("/api/v1/gpc/search/", params={"q": "%"}).json()
        assert len(wild["bricks"]) <= 50
        assert wild["truncated"] is True
    finally:
        import asyncio
        if database._db is not None:
            asyncio.run(database._db.close())
        database._db = None
