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


def test_off_microgram_scale_nutrients_are_converted():
    """The µg-scale extension (vitamin A/K, folate, B12, biotin, selenium,
    molybdenum) needs the same gram -> published-unit scaling as vitamin D,
    just with a bigger multiplier: 1,000,000, not 1,000."""
    values = from_off({"folate": 0.00017, "vitamin_b12": 0.0000009, "selenium": 0.00004})

    assert values["folate"] == pytest.approx(170.0)
    assert values["vitamin_b12"] == pytest.approx(0.9)
    assert values["selenium"] == pytest.approx(40.0)


def test_off_milligram_scale_extension_nutrients_are_converted():
    values = from_off({"magnesium": 0.073, "zinc": 0.02273, "copper": 0.002})

    assert values["magnesium"] == pytest.approx(73.0)
    assert values["zinc"] == pytest.approx(22.73)
    assert values["copper"] == pytest.approx(2.0)


def test_off_non_numeric_values_are_dropped():
    """OFF is crowdsourced: ">100" and "trace" arrive off real labels."""
    assert from_off({"protein": ">100", "fat": 30.9}) == {"fat": 30.9}


def test_off_missing_nutrients_are_absent_not_zero():
    values = from_off({"protein": 6.3})

    assert values == {"protein": 6.3}
    assert "sodium" not in values          # absent, not 0


# ══ The published panel ═══════════════════════════════════════════════

def test_the_panel_covers_the_us_nutrition_facts_label():
    """The mandatory label panel must be a subset of what we publish, rather
    than an arbitrary approximation of it."""
    fields = {spec.field for spec in NUTRIENTS}

    assert fields >= {
        "calories_kcal", "protein", "fat", "saturated_fat", "trans_fat",
        "cholesterol", "carbohydrates", "fiber", "sugars", "added_sugars",
        "sodium", "potassium", "calcium", "iron", "vitamin_d",
    }


def test_the_panel_covers_every_vitamin_and_mineral_beyond_the_label():
    """The vitamin/mineral extension beyond the mandatory label panel --
    pinned exactly, so an addition or removal here is a deliberate act."""
    fields = {spec.field for spec in NUTRIENTS}

    assert fields == {
        "calories_kcal", "protein", "fat", "saturated_fat", "trans_fat",
        "cholesterol", "carbohydrates", "fiber", "sugars", "added_sugars",
        "sodium", "potassium", "calcium", "iron", "vitamin_d",
        "vitamin_a", "vitamin_c", "vitamin_e", "vitamin_k",
        "thiamin", "riboflavin", "niacin", "vitamin_b6", "folate",
        "vitamin_b12", "pantothenic_acid", "biotin", "choline",
        "magnesium", "zinc", "phosphorus", "selenium", "copper",
        "manganese", "molybdenum", "caffeine",
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


# ══ The vitamin D IU / µg collision ═══════════════════════════════════
#
# The same failure as energy, wearing a different unit. FDC carries vitamin D
# under two ids and nothing in the payload says they are incompatible:
#
#     1114  Vitamin D (D2 + D3)                        UG
#     1110  Vitamin D (D2 + D3), International Units   IU     1 µg = 40 IU
#
# They were listed as plain fallbacks and whichever turned up was published as
# micrograms. In the April 2026 branded corpus 562,567 foods carry only 1110
# against 128,397 carrying only 1114, and just 111 carry both — so the
# preference order almost never saved us, and four fifths of all vitamin D we
# served was 40x too high.

def test_vitamin_d_in_iu_is_converted_not_taken_at_face_value():
    """400 IU is a normal fortified serving: 10 µg. It was published as 400 µg."""
    values = from_usda([usda(1110, "Vitamin D (D2 + D3), International Units",
                             400.0, "IU")])

    assert values["vitamin_d"] == 10.0          # not 400.0


def test_vitamin_d_in_micrograms_is_left_alone():
    values = from_usda([usda(1114, "Vitamin D (D2 + D3)", 10.0, "UG")])

    assert values["vitamin_d"] == 10.0


def test_micrograms_win_when_a_food_carries_both_ids():
    """111 foods in the corpus do. The µg id is the one we trust."""
    values = from_usda([
        usda(1110, "Vitamin D (D2 + D3), International Units", 400.0, "IU"),
        usda(1114, "Vitamin D (D2 + D3)", 10.0, "UG"),
    ])

    assert values["vitamin_d"] == 10.0


def test_vitamin_d_conversion_holds_whatever_the_order():
    both_orders = [
        from_usda([usda(1114, "Vitamin D", 10.0, "UG"),
                   usda(1110, "Vitamin D IU", 400.0, "IU")]),
        from_usda([usda(1110, "Vitamin D IU", 400.0, "IU"),
                   usda(1114, "Vitamin D", 10.0, "UG")]),
    ]

    assert [v["vitamin_d"] for v in both_orders] == [10.0, 10.0]


@pytest.mark.parametrize("spelling", ["IU", "iu", " Iu "])
def test_the_iu_unit_is_recognised_however_it_is_spelled(spelling):
    values = from_usda([usda(1110, "Vitamin D IU", 400.0, spelling)])

    assert values["vitamin_d"] == 10.0


@pytest.mark.parametrize("spelling", ["UG", "ug", "µg", "MCG"])
def test_the_microgram_unit_is_recognised_however_it_is_spelled(spelling):
    values = from_usda([usda(1114, "Vitamin D", 10.0, spelling)])

    assert values["vitamin_d"] == 10.0


# ══ The vitamin A IU / µg RAE collision ════════════════════════════════
#
# The same two-id split as vitamin D (1106 in µg RAE, 1104 in IU), but the
# conversion is an approximation, not a physical constant -- see the
# IU_PER_UG_RAE_VITAMIN_A comment in nutrients.py for why. 0.3 µg RAE per IU
# is the FDA's own historical label conversion factor.

def test_vitamin_a_in_iu_is_converted_not_taken_at_face_value():
    values = from_usda([usda(1104, "Vitamin A, IU", 1000.0, "IU")])

    assert values["vitamin_a"] == 300.0          # not 1000.0


def test_vitamin_a_in_rae_micrograms_is_left_alone():
    values = from_usda([usda(1106, "Vitamin A, RAE", 300.0, "UG")])

    assert values["vitamin_a"] == 300.0


def test_rae_micrograms_win_when_a_food_carries_both_vitamin_a_ids():
    """RAE is the direct measurement; IU is the approximation. Direct wins."""
    values = from_usda([
        usda(1104, "Vitamin A, IU", 1000.0, "IU"),
        usda(1106, "Vitamin A, RAE", 300.0, "UG"),
    ])

    assert values["vitamin_a"] == 300.0


# ══ Copper: mg on the wire, not µg ══════════════════════════════════════
#
# nutrimetrics' own display_unit for copper is micrograms (chosen for its DRI
# comparison, where the 900 µg RDA is the natural unit to show) but FDC's id
# 1098 actually arrives in milligrams -- confirmed against a live FDC payload.
# Publishing an FDC "mg" value under a "µg" label would overstate copper
# 1000x, the same class of error the rest of this module exists to prevent.

def test_copper_is_published_in_milligrams():
    values = from_usda([usda(1098, "Copper, Cu", 2.0, "MG")])

    assert values["copper"] == 2.0


def test_copper_in_the_wrong_unit_is_skipped_not_published():
    values = from_usda([usda(1098, "Copper, Cu", 2.0, "UG")])

    assert "copper" not in values


# ══ The unit is checked, not assumed ══════════════════════════════════

def test_a_nutrient_in_an_unexpected_unit_is_skipped_not_published():
    """Publishing an unknown unit's number under our own label is the whole bug.

    If FDC ever hands us protein in milligrams, dropping it is honest.
    Publishing "0.5 g" when it means 0.5 mg is not.
    """
    values = from_usda([usda(1003, "Protein", 500.0, "MG")])

    assert "protein" not in values


def test_a_contradictory_unit_falls_through_to_the_next_id():
    """Fat is (1004, 1085). A bad 1004 must not shadow a good 1085."""
    values = from_usda([
        usda(1004, "Total lipid (fat)", 9999.0, "KJ"),   # nonsense unit
        usda(1085, "Total fat (NLEA)", 3.5, "G"),
    ])

    assert values["fat"] == 3.5


def test_an_entry_with_no_declared_unit_is_still_accepted():
    """We cannot check what we were not told, and good data shouldn't be lost."""
    values = from_usda([usda(1003, "Protein", 7.0, None)])

    assert values["protein"] == 7.0


def test_energy_in_kj_is_only_converted_when_it_really_is_kj():
    """A 1062 that claims to be kcal is not something we understand."""
    values = from_usda([usda(1062, "Energy", 1710.0, "KCAL")])

    assert "calories_kcal" not in values


# ══ Values that cannot physically exist ═══════════════════════════════
#
# 100 g of food contains at most 100 g of anything, and fat — the densest
# macronutrient at 9 kcal/g — caps energy near 900 kcal per 100 g. Upstream data
# breaks both. 2,545 products in the April 2026 branded corpus (0.58%) carry an
# impossible value: a burrito at 90,000 kcal and 12,700 g of carbohydrate per
# 100 g, a drink mix at 151,515 kcal. They read like per-package figures filed as
# per-100 g. A number that cannot exist is not data, and serving it is worse than
# serving nothing.

def test_a_burrito_cannot_contain_12700_grams_of_carbohydrate():
    """The real record, from FDC. Per 100 g there are only 100 g to go around."""
    values = from_usda([
        usda(1005, "Carbohydrate, by difference", 12700.0, "G"),
        usda(1003, "Protein", 3400.0, "G"),
    ])

    assert "carbohydrates" not in values
    assert "protein" not in values


def test_energy_above_the_physical_ceiling_is_dropped():
    """Pure fat is ~900 kcal/100 g. 151,515 is not a food."""
    assert "calories_kcal" not in from_usda([usda(1008, "Energy", 151515.0, "KCAL")])


def test_energy_at_the_ceiling_is_kept():
    """Pure oil really is ~900 kcal/100 g — the guard must not eat real food."""
    assert from_usda([usda(1008, "Energy", 900.0, "KCAL")])["calories_kcal"] == 900.0


def test_a_negative_nutrient_is_impossible_too():
    assert "protein" not in from_usda([usda(1003, "Protein", -5.0, "G")])


def test_an_impossible_value_falls_through_to_a_usable_id():
    """Fat is (1004, 1085). A nonsense 1004 must not shadow a real 1085."""
    values = from_usda([
        usda(1004, "Total lipid (fat)", 3200.0, "G"),   # impossible
        usda(1085, "Total fat (NLEA)", 32.0, "G"),      # the real figure
    ])

    assert values["fat"] == 32.0


def test_the_guard_holds_for_milligrams_and_micrograms():
    """31,818,182 mg of sodium is 31 kg of salt in 100 g of food."""
    assert "sodium" not in from_usda([usda(1093, "Sodium, Na", 31_818_182.0, "MG")])
    assert from_usda([usda(1093, "Sodium, Na", 400.0, "MG")])["sodium"] == 400.0


def test_impossible_crowdsourced_values_are_dropped_as_well():
    """Open Food Facts is no cleaner than FDC, and goes through the same gate.

    OFF publishes in grams, so 500 g of protein per 100 g arrives as 500.0.
    """
    assert "protein" not in from_off({"protein": 500.0})
    assert from_off({"protein": 7.0})["protein"] == 7.0


# ══ Energy that contradicts fat and protein ═══════════════════════════
#
# Fat and protein always carry their calories — 9 and 4 kcal/g — with no
# calorie-free exception (only carbohydrate has one, via sugar alcohols). So a
# food's stated energy cannot fall far below what its fat and protein alone must
# contribute. When it does, the two figures physically contradict each other,
# and the error is invisible to the per-value guard because each number is
# individually possible:
#
#     Nutella  0 kcal / 30.9 g fat   — the fat alone is ~278 kcal
#
# This is the physical-maximum guard lifted from a single value to a
# relationship. It matters most for the 2.2M single-source OFF products that have
# nothing to correct them. The macros set the floor and are kept; the energy —
# the value provably beneath it — is dropped.

def test_zero_energy_with_real_fat_is_dropped():
    """Nutella filed at 0 kcal with 30.9 g of fat. The fat cannot be free."""
    values = from_off({"calories_kcal": 0.0, "fat": 30.9, "protein": 6.3})

    assert "calories_kcal" not in values
    assert values["fat"] == 30.9          # the macros are kept


def test_impossibly_low_energy_for_a_fatty_food_is_dropped():
    """Mayonnaise at 5 kcal with 16 g fat — fat alone is ~144 kcal."""
    values = from_off({"calories_kcal": 5.0, "fat": 16.0, "protein": 1.0})

    assert "calories_kcal" not in values


def test_a_real_energy_is_left_alone():
    """Whole milk: 61 kcal, 3.3 g fat, 3.2 g protein. Floor ~43, well under 61."""
    values = from_off({"calories_kcal": 61.0, "fat": 3.3, "protein": 3.2})

    assert values["calories_kcal"] == 61.0


def test_a_near_zero_food_is_never_judged():
    """A diet drink genuinely is ~0 kcal. With almost no fat or protein its floor
    is near zero, so its low energy must not be second-guessed."""
    values = from_off({"calories_kcal": 1.0, "fat": 0.0, "protein": 0.0,
                       "sugars": 0.0})

    assert values["calories_kcal"] == 1.0


def test_the_carbohydrate_loophole_is_respected():
    """Sugar-free products declare ~0 kcal against real carbs (polyols carry
    carbohydrate but little energy). Carbs are deliberately outside the floor, so
    these are never flagged."""
    values = from_off({"calories_kcal": 0.0, "carbohydrates": 90.0,
                       "fat": 0.0, "protein": 0.0})

    assert values["calories_kcal"] == 0.0


def test_a_mild_shortfall_from_rounding_is_tolerated():
    """The threshold is a quarter of the floor, so ordinary rounding and
    drained-oil quirks survive: 89 kcal against a 175 kcal floor is kept."""
    values = from_off({"calories_kcal": 89.0, "fat": 17.86, "protein": 3.57})

    assert values["calories_kcal"] == 89.0


def test_the_same_guard_applies_to_usda():
    """It is a physical law, not an OFF quirk — milk chocolate at 58 kcal with
    36 g fat is a real FDC error, and gets the same treatment."""
    values = from_usda([
        usda(1008, "Energy", 58.0, "KCAL"),
        usda(1004, "Total lipid (fat)", 36.0, "G"),
        usda(1003, "Protein", 6.0, "G"),
    ])

    assert "calories_kcal" not in values
    assert values["fat"] == 36.0


# ══ A component cannot exceed the whole it belongs to ═════════════════
#
# Saturated and trans fat are fractions of total fat; sugars a fraction of
# carbohydrate; added sugars a fraction of total sugars. A child above its
# parent is definitionally impossible, and — like the energy floor — invisible
# to the per-value guard because each number is fine on its own. The child is
# the suspect (the breakdown line, more often mistyped); the parent is kept.

def test_saturated_fat_above_total_fat_is_dropped():
    """A protein pudding filed with 76 g saturated fat but 12 g total fat."""
    values = from_off({"fat": 12.3, "saturated_fat": 76.7})

    assert "saturated_fat" not in values
    assert values["fat"] == 12.3


def test_added_sugars_above_total_sugars_is_dropped():
    """The commonest structural error in OFF — 3.3% of products."""
    values = from_off({"sugars": 2.0, "added_sugars": 61.4})

    assert "added_sugars" not in values
    assert values["sugars"] == 2.0


def test_sugars_above_carbohydrate_is_dropped():
    values = from_off({"carbohydrates": 10.0, "sugars": 25.0})

    assert "sugars" not in values
    assert values["carbohydrates"] == 10.0


def test_a_real_breakdown_survives():
    """Sugars 10 of 40 carbs, added 6 of those 10, sat fat 3 of 5 — all valid."""
    values = from_off({"carbohydrates": 40.0, "sugars": 10.0, "added_sugars": 6.0,
                       "fat": 5.0, "saturated_fat": 3.0})

    assert values["sugars"] == 10.0
    assert values["added_sugars"] == 6.0
    assert values["saturated_fat"] == 3.0


def test_equal_to_the_parent_is_allowed():
    """A food that is pure sugar: sugars == carbohydrate is legitimate."""
    values = from_off({"carbohydrates": 100.0, "sugars": 100.0})

    assert values["sugars"] == 100.0


def test_rounding_slack_keeps_a_marginal_child():
    """Label rounding can nudge a child a hair over; 2% slack tolerates it."""
    values = from_off({"fat": 10.0, "saturated_fat": 10.1})

    assert values["saturated_fat"] == 10.1


def test_fibre_is_never_compared_to_carbohydrate():
    """US labels count fibre inside carbohydrate, EU labels report it apart, so
    fibre above carbohydrate is normal for OFF's European data — not an error."""
    values = from_off({"carbohydrates": 5.0, "fiber": 40.0})

    assert values["fiber"] == 40.0
    assert values["carbohydrates"] == 5.0


def test_subset_check_applies_to_usda_too():
    values = from_usda([
        usda(1004, "Total lipid (fat)", 5.0, "G"),
        usda(1258, "Fatty acids, total saturated", 20.0, "G"),
    ])

    assert "saturated_fat" not in values
    assert values["fat"] == 5.0
