"""
Tests for collapsing FDC's revisions down to one row per barcode.

A GTIN is not unique in FDC. It republishes a product as a brand new fdc_id
whenever the label changes, so 2.0M branded records in the April 2026 corpus are
really only 442,095 distinct barcodes — 4.5 revisions each on average, and as
many as 38. They disagree: 31% of colliding barcodes report different calories.

Which revision we serve therefore cannot be left to chance, and this is the rule:

  * the newest revision defines the product — its identity and every nutrient it
    declares;
  * where the newest revision is *silent* about a nutrient, an earlier one may
    fill the gap. FDC's revisions are frequently partial, and a missing figure is
    a hole in the paperwork, not a claim that the food contains none of it. This
    recovers 122,205 nutrient values across the corpus.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from build_fdc_db import NUTRIENT_FIELDS, _build_fts_table, _build_served_table  # noqa: E402


def staged(revisions):
    """An in-memory database with the staging tables one build step leaves behind.

    `revisions` is a list of (fdc_id, gtin14, published, description, nutrients),
    where nutrients maps our field names to amounts.
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stg_food (fdc_id INTEGER PRIMARY KEY, "
               "description TEXT, published TEXT)")
    db.execute("""CREATE TABLE stg_branded (
        fdc_id INTEGER PRIMARY KEY, gtin14 TEXT, brand_owner TEXT,
        brand_name TEXT, ingredients TEXT, serving_size REAL,
        serving_size_unit TEXT, household_serving TEXT, category TEXT)""")
    columns = ", ".join(f"{f} REAL" for f in NUTRIENT_FIELDS)
    db.execute(f"CREATE TABLE stg_nutrients (fdc_id INTEGER PRIMARY KEY, {columns})")

    for fdc_id, gtin14, published, description, nutrients in revisions:
        db.execute("INSERT INTO stg_food VALUES (?,?,?)",
                   (fdc_id, description, published))
        db.execute("INSERT INTO stg_branded VALUES (?,?,?,?,?,?,?,?,?)",
                   (fdc_id, gtin14, "Brand Co", None, "ingredients",
                    100.0, "g", "1 cup", "Category"))
        placeholders = ",".join("?" * (len(NUTRIENT_FIELDS) + 1))
        db.execute(
            f"INSERT INTO stg_nutrients VALUES ({placeholders})",
            (fdc_id, *(nutrients.get(f) for f in NUTRIENT_FIELDS)),
        )
    db.commit()
    return db


def served(db, gtin14):
    db.row_factory = sqlite3.Row
    return db.execute("SELECT * FROM foods WHERE gtin14 = ?", (gtin14,)).fetchone()


def test_the_newest_revision_defines_the_product():
    """The potato chips: one bag, republished three times over two years."""
    db = staged([
        (359586, "00001", "2019-04-01", "POTATO CHIPS", {"calories_kcal": 520.0}),
        (1759872, "00001", "2021-06-17", "POTATO CHIPS, SEA SALT",
         {"calories_kcal": 530.0}),
        (1850914, "00001", "2021-07-29", "POTATO CHIPS, SEA SALT",
         {"calories_kcal": 536.0}),
    ])

    count, _ = _build_served_table(db)
    row = served(db, "00001")

    assert count == 1                          # three records, one barcode
    assert row["fdc_id"] == 1850914            # the 2021-07-29 revision
    assert row["calories_kcal"] == 536.0       # not 520, not whichever came first


def test_a_gap_in_the_newest_revision_is_filled_from_an_earlier_one():
    """The real case: the newest tomatoes declare calories but no protein."""
    db = staged([
        (344604, "00002", "2019-04-01", "Diced Tomatoes",
         {"calories_kcal": 24.0, "protein": 0.83}),
        (750854, "00002", "2020-02-27", "Diced Tomatoes, No Salt Added",
         {"calories_kcal": 33.0}),                  # silent about protein
    ])

    _build_served_table(db)
    row = served(db, "00002")

    assert row["calories_kcal"] == 33.0        # newest wins where it speaks
    assert row["protein"] == 0.83              # and an older one fills the silence


def test_an_older_revision_never_overrides_a_value_the_newest_declares():
    """Silence may be filled. A stated figure may not be contradicted."""
    db = staged([
        (1, "00003", "2019-01-01", "Cereal", {"calories_kcal": 400.0, "sugars": 30.0}),
        (2, "00003", "2025-01-01", "Cereal, Reformulated",
         {"calories_kcal": 350.0, "sugars": 12.0}),
    ])

    _build_served_table(db)
    row = served(db, "00003")

    assert row["calories_kcal"] == 350.0
    assert row["sugars"] == 12.0               # the reformulation, not the old recipe


def test_a_nutrient_no_revision_declares_stays_unknown():
    """Absent is not zero. We must not invent a figure nobody published."""
    db = staged([
        (1, "00004", "2025-01-01", "Water", {"calories_kcal": 0.0}),
    ])

    _build_served_table(db)
    row = served(db, "00004")

    assert row["calories_kcal"] == 0.0         # a real, declared zero
    assert row["vitamin_d"] is None            # never declared -> unknown


def test_same_day_revisions_break_the_tie_on_fdc_id():
    """1.3% of collisions are published on the same day. The later record wins."""
    db = staged([
        (100, "00005", "2026-04-30", "Older record", {"calories_kcal": 100.0}),
        (900, "00005", "2026-04-30", "Later record", {"calories_kcal": 200.0}),
    ])

    _build_served_table(db)
    row = served(db, "00005")

    assert row["fdc_id"] == 900
    assert row["calories_kcal"] == 200.0


def test_the_gap_fill_count_is_reported():
    """The build reports what it recovered — 122,205 values across the corpus."""
    db = staged([
        (1, "00006", "2019-01-01", "Snack", {"protein": 5.0, "iron": 2.0}),
        (2, "00006", "2025-01-01", "Snack", {"calories_kcal": 500.0}),
    ])

    _, filled = _build_served_table(db)

    assert filled == 2                         # protein and iron


def test_many_barcodes_are_collapsed_independently():
    db = staged([
        (1, "00007", "2020-01-01", "A", {"calories_kcal": 10.0}),
        (2, "00007", "2025-01-01", "A", {"calories_kcal": 20.0}),
        (3, "00008", "2021-01-01", "B", {"calories_kcal": 30.0}),
    ])

    count, _ = _build_served_table(db)

    assert count == 2
    assert served(db, "00007")["calories_kcal"] == 20.0
    assert served(db, "00008")["calories_kcal"] == 30.0


@pytest.mark.parametrize("revisions", [1, 2, 5, 38])
def test_a_barcode_yields_exactly_one_row_however_many_revisions_it_has(revisions):
    """38 is the worst real case in the corpus."""
    db = staged([
        (i, "00009", f"20{10 + i:02d}-01-01", f"rev {i}", {"calories_kcal": float(i)})
        for i in range(1, revisions + 1)
    ])

    count, _ = _build_served_table(db)

    assert count == 1
    assert served(db, "00009")["calories_kcal"] == float(revisions)


# ── _build_fts_table ─────────────────────────────────────────────────

def test_fts_table_is_searchable_by_prefix_after_the_served_table_is_built():
    db = staged([
        (1, "00010", "2026-01-01", "Diced Tomatoes", {"calories_kcal": 20.0}),
        (2, "00011", "2026-01-01", "Raisin Bran Cereal", {"calories_kcal": 300.0}),
    ])
    _build_served_table(db)

    _build_fts_table(db)

    row = db.execute(
        "SELECT gtin14 FROM foods_fts WHERE foods_fts MATCH ?", ('"toma"*',)
    ).fetchone()
    assert row == ("00010",)


def test_fts_table_gtin_is_not_indexed_as_searchable_text():
    """gtin14 is UNINDEXED -- present in the row for the join back to foods,
    but not itself matchable text (searching the barcode's digits as if they
    were a product-name word would be surprising, and FTS5's default tokenizer
    would not usefully index them anyway)."""
    db = staged([
        (1, "00012", "2026-01-01", "Snack Food", {"calories_kcal": 10.0}),
    ])
    _build_served_table(db)

    _build_fts_table(db)

    rows = db.execute(
        "SELECT gtin14 FROM foods_fts WHERE foods_fts MATCH ?", ('"00012"*',)
    ).fetchall()
    assert rows == []
