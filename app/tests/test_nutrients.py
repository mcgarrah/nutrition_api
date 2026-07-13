"""
Tests for nutrient identity: which FDC nutrient is which, and in what unit.

The bug these exist to prevent is the worst kind this service can have — a
plausible number that is simply wrong. FDC publishes energy twice, in kcal
(id 1008) and kJ (id 1062), under the *identical* name "Energy". Collapsing the
nutrient list into a dict keyed by name keeps whichever arrived last:

    Cheese, cheddar   id=1008   408 kcal
                      id=1062  1710 kJ     <- arrived last, so it won

and the API reported cheddar at **1710 kcal per 100 g**, four times its real
energy. Across a sample of Foundation foods, roughly a quarter put the kJ entry
last. Nothing in FDC's contract promises an order, so which number a caller got
was decided by luck.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest

from app.core import nutrients
from app.core.nutrients import NUTRIENTS, from_off, from_usda


def usda(fdc_id, name, amount, unit):
    return {"id": fdc_id, "name": name, "amount": amount, "unit": unit}


# ══ The kJ / kcal collision ═══════════════════════════════════════════

def test_energy_is_taken_from_the_kcal_id_not_the_last_entry():
    """The real cheddar payload, in FDC's real order — kJ last."""
    values = from_usda([
        usda(1008, "Energy", 408.0, "KCAL"),
        usda(1062, "Energy", 1710.0, "kJ"),      # same name, arrives last
    ])

    assert values["calories_kcal"] == 408.0      # not 1710


def test_energy_is_correct_whatever_the_order():
    """Nothing in the FDC contract promises an order, so neither may we."""
    kcal_last = from_usda([
        usda(1062, "Energy", 1710.0, "kJ"),
        usda(1008, "Energy", 408.0, "KCAL"),
    ])
    kj_last = from_usda([
        usda(1008, "Energy", 408.0, "KCAL"),
        usda(1062, "Energy", 1710.0, "kJ"),
    ])

    assert kcal_last["calories_kcal"] == kj_last["calories_kcal"] == 408.0


def test_kilojoules_alone_are_converted_not_reported_as_calories():
    """A food that only reports kJ still deserves an answer — just not that
    number with a kcal label on it."""
    values = from_usda([usda(1062, "Energy", 1710.0, "kJ")])

    assert values["calories_kcal"] == pytest.approx(408.7, abs=0.5)


def test_the_kilojoule_id_is_never_treated_as_calories():
    """1062 must not appear among the energy ids, or the whole guard is moot."""
    energy = next(s for s in NUTRIENTS if s.field == "calories_kcal")

    assert nutrients.ENERGY_KJ_ID == 1062
    assert 1062 not in energy.fdc_ids
    assert 1008 in energy.fdc_ids


def test_atwater_energy_variants_are_accepted():
    """FDC also publishes energy under the Atwater ids — still kilocalories."""
    assert from_usda([usda(2047, "Energy (Atwater General)", 400.0, "KCAL")])[
        "calories_kcal"] == 400.0
    assert from_usda([usda(2048, "Energy (Atwater Specific)", 401.0, "KCAL")])[
        "calories_kcal"] == 401.0


# ══ Selection by id, not name ═════════════════════════════════════════

def test_nutrients_are_found_by_id_even_when_the_name_is_unfamiliar():
    """FDC's names drift; the ids do not. Matching on the name means a rename
    upstream silently drops the nutrient."""
    values = from_usda([
        usda(1003, "Protein (renamed upstream)", 7.1, "G"),
        usda(1093, "Sodium (renamed upstream)", 530.0, "MG"),
    ])

    assert values["protein"] == 7.1
    assert values["sodium"] == 530.0


def test_an_unknown_nutrient_id_is_ignored():
    assert from_usda([usda(99999, "Unobtainium", 1.0, "G")]) == {}


def test_a_nutrient_without_an_amount_is_skipped():
    assert from_usda([usda(1003, "Protein", None, "G")]) == {}


def test_malformed_entries_do_not_break_the_scan():
    values = from_usda([
        "not a dict",
        {"no": "id"},
        usda(1003, "Protein", 7.1, "G"),
    ])

    assert values["protein"] == 7.1


def test_the_first_matching_id_wins():
    """fdc_ids are in preference order — fat prefers 1004 over 1085."""
    values = from_usda([
        usda(1085, "Total fat (NLEA)", 9.0, "G"),
        usda(1004, "Total lipid (fat)", 10.0, "G"),
    ])

    assert values["fat"] == 10.0


# ══ Open Food Facts units ═════════════════════════════════════════════

def test_off_grams_are_converted_to_the_unit_we_publish():
    """OFF reports every nutrient in grams, including those a label shows in
    milligrams: sodium comes back as 0.0428, not 42.8. Publishing that number
    unconverted understates sodium a thousandfold."""
    values = from_off({"sodium": 0.0428, "calcium": 0.12, "iron": 0.0004})

    assert values["sodium"] == pytest.approx(42.8)
    assert values["calcium"] == pytest.approx(120.0)
    assert values["iron"] == pytest.approx(0.4)


def test_off_gram_nutrients_pass_through_unscaled():
    values = from_off({"protein": 6.3, "fat": 30.9, "carbohydrates": 57.5})

    assert values["protein"] == 6.3
    assert values["fat"] == 30.9
    assert values["carbohydrates"] == 57.5


def test_off_non_numeric_values_are_dropped():
    """OFF is crowdsourced: ">100" and "trace" arrive off real labels."""
    assert from_off({"protein": ">100", "fat": 30.9}) == {"fat": 30.9}


def test_off_missing_nutrients_are_absent_not_zero():
    values = from_off({"protein": 6.3})

    assert values == {"protein": 6.3}
    assert "sodium" not in values          # absent, not 0


# ══ The published panel ═══════════════════════════════════════════════

def test_the_panel_covers_the_us_nutrition_facts_label():
    """What a consumer expects to find on a label, rather than an arbitrary
    subset of it."""
    fields = {spec.field for spec in NUTRIENTS}

    assert fields == {
        "calories_kcal", "protein", "fat", "saturated_fat", "trans_fat",
        "cholesterol", "carbohydrates", "fiber", "sugars", "added_sugars",
        "sodium", "potassium", "calcium", "iron", "vitamin_d",
    }


def test_every_nutrient_declares_an_id_a_key_and_a_unit():
    for spec in NUTRIENTS:
        assert spec.fdc_ids, spec.field
        assert spec.off_key, spec.field
        assert spec.unit, spec.field


def test_no_two_nutrients_claim_the_same_fdc_id():
    """A shared id would make one nutrient shadow another."""
    seen: dict[int, str] = {}
    for spec in NUTRIENTS:
        for fdc_id in spec.fdc_ids:
            assert fdc_id not in seen, f"{fdc_id} claimed by {seen.get(fdc_id)} and {spec.field}"
            seen[fdc_id] = spec.field
