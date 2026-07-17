"""
Nutrient identity: which FDC nutrient is which, and in what unit.

Matching USDA nutrients by their *name* is unsafe, and not in a theoretical
way. FDC publishes energy twice — as kilocalories (id 1008) and kilojoules
(id 1062) — under the identical name "Energy". Collapsing the nutrient list
into a dict keyed by name therefore keeps whichever happened to arrive last:

    Cheese, cheddar   ->  Energy 1710 kJ   (id 1062)   <- last, so it won
                          Energy  409 kcal (id 1008)

which is how a cheese ends up reported at 1710 kcal per 100 g — four times its
real energy. Across a sample of Foundation foods, roughly a quarter put the kJ
entry last. Nothing in the FDC contract promises an order, so the value we
served was decided by luck.

Nutrients are therefore selected by **id**, and the unit is checked rather than
assumed. The id groupings follow those in `nutrimetrics`
(https://github.com/mcgarrah/nutrimetrics), which has the same discipline of
listing only the kcal energy ids and never 1062 — cross-checked against real
FDC and Open Food Facts payloads before being added, not ported blindly: one
id here (copper, 1098) turned out to differ from what `nutrimetrics` itself
declares, because its `display_unit` is chosen for its own DRI-comparison
workbook rather than for what FDC actually returns on the wire.

The set below is the **US Nutrition Facts panel plus every vitamin and
mineral** `nutrimetrics` tracks — the macros a label shows, and the
micronutrient panel beyond it. Individual amino acids and sugar-type
breakdowns (starch, sucrose, glucose, ...) are deliberately out of scope: this
is a food-lookup API, not a nutrition-analysis tool, and that level of detail
is rarely populated for branded, UPC-scanned foods even when we ask for it.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NutrientSpec:
    """How to find one nutrient in an FDC food, and what unit we publish it in."""

    field: str              # the CanonicalProduct field it populates
    fdc_ids: tuple[int, ...]  # FDC nutrient ids, in preference order
    off_key: str            # the Open Food Facts per-100g key
    unit: str               # the unit WE publish, regardless of the source's


# Energy in kilojoules. Deliberately *not* in ENERGY's id list: it shares the
# name "Energy" with the kcal entry, and treating it as interchangeable is the
# bug this module exists to prevent. Kept only so a food that reports energy
# solely in kJ can still be converted.
ENERGY_KJ_ID = 1062
KJ_PER_KCAL = 4.184

# Vitamin D is the same trap in a different unit. FDC carries it under two ids —
# 1114 in micrograms and 1110 in *International Units* — and nothing in the
# payload warns that they are not interchangeable: 1 µg = 40 IU. Listing them as
# plain fallbacks and publishing whichever turns up overstates vitamin D by 40x.
#
# This is not an edge case. In the April 2026 branded corpus, 562,567 foods carry
# only 1110 against 128,397 carrying only 1114 — the IU id outnumbers the µg id
# more than four to one, and just 111 foods carry both — so the preference order
# almost never rescues us. A fortified milk at 400 IU (a normal 10 µg serving)
# went out as 400 µg: 2,667% of the daily value.
IU_PER_UG_VITAMIN_D = 40.0

# Vitamin A carries the same two-id split (1106 in µg RAE, 1104 in IU), but the
# conversion is NOT the clean physical constant vitamin D's is. Vitamin D's
# 40 IU/µg holds for cholecalciferol regardless of source; vitamin A's IU mixes
# preformed retinol (potent) with provitamin-A carotenoids (much less potent
# per IU), so the "right" IU->RAE ratio genuinely depends on what is in the
# food. There is no exact answer available from the FDC payload alone.
#
# 0.3 µg RAE per IU is not invented here: it is the FDA's own historical label
# conversion factor (21 CFR 101.9(c)(8)(iv), the pre-2016 Nutrition Facts
# standard), and it is a reasonable approximation specifically for *branded,
# fortified* foods, whose added vitamin A is preformed retinyl palmitate/
# acetate rather than carotenoids -- which is the food population this API
# actually serves. It is an approximation nonetheless, unlike vitamin D's exact
# factor, and is applied only when 1106 is absent.
IU_PER_UG_RAE_VITAMIN_A = 1.0 / 0.3

# What FDC declares each id in, so we can convert rather than assume. Anything
# whose declared unit already matches the unit we publish needs no entry here.
_FDC_SCALE: dict[int, float] = {
    1110: 1.0 / IU_PER_UG_VITAMIN_D,  # IU -> µg
    1104: 1.0 / IU_PER_UG_RAE_VITAMIN_A,  # IU -> µg RAE (approximate, see above)
}

# The unit FDC is expected to declare for each id we read. An entry arriving in
# some *other* unit is a nutrient we do not understand, and publishing its number
# under our own unit label would be exactly the failure this module exists to
# prevent — so we skip it and fall through to the next id instead.
_FDC_UNIT: dict[int, str] = {
    1008: "KCAL", 2047: "KCAL", 2048: "KCAL", 1062: "KJ",
    1003: "G", 1004: "G", 1085: "G", 1258: "G", 1257: "G", 1005: "G",
    1079: "G", 2033: "G", 2000: "G", 1063: "G", 1235: "G",
    1253: "MG", 1093: "MG", 1092: "MG", 1087: "MG", 1089: "MG",
    1114: "UG", 1110: "IU",
    # Vitamins
    1106: "UG", 1104: "IU", 1162: "MG", 1109: "MG", 1185: "UG",
    1165: "MG", 1166: "MG", 1167: "MG", 1175: "MG", 1177: "UG",
    1178: "UG", 1170: "MG", 1176: "UG", 1180: "MG",
    # Minerals
    1090: "MG", 1095: "MG", 1091: "MG", 1103: "UG", 1098: "MG",
    1101: "MG", 1102: "UG",
    # Other
    1057: "MG",
}

# FDC is not consistent about how it spells a unit, and neither is the SDK.
_UNIT_ALIASES = {"MCG": "UG"}

# The micro sign (U+00B5) and Greek small mu (U+03BC) both show up in the wild,
# and neither survives .upper() intact — Python turns "µg" into "ΜG" with a Greek
# capital Mu, which matches nothing. Fold them to a plain "u" before casefolding.
_MICRO_SIGNS = str.maketrans({"µ": "u", "μ": "u"})


# The most of any nutrient that can exist in 100 g of food, in the unit we
# publish. These are physical ceilings, not opinions: 100 g of food contains at
# most 100 g of anything, and fat — the most energy-dense macronutrient at
# 9 kcal/g — caps energy at about 900 kcal per 100 g.
#
# Upstream data violates them. In the April 2026 branded corpus 2,545 products
# (0.58%) carry at least one impossible value: a burrito at 90,000 kcal and
# 12,700 g of carbohydrate per 100 g, a drink mix at 151,515 kcal. They look like
# per-package figures filed as per-100 g. A number that cannot exist is not
# nutrition data, and serving it is worse than serving nothing — so we drop it
# and report the nutrient as absent.
_GRAMS_IN_100G = 100.0
_MG_IN_100G = 100_000.0
_UG_IN_100G = 100_000_000.0
_MAX_KCAL_PER_100G = 902.0

_PHYSICAL_MAX: dict[str, float] = {
    "kcal": _MAX_KCAL_PER_100G,
    "g": _GRAMS_IN_100G,
    "mg": _MG_IN_100G,
    "µg": _UG_IN_100G,
}


def is_physically_possible(field: str, amount: float) -> bool:
    """Could 100 g of food really contain this much of this nutrient?

    A negative amount is impossible too — and FDC does publish those.
    """
    spec = _BY_FIELD.get(field)
    if spec is None:
        return True
    if amount < 0:
        return False
    ceiling = _PHYSICAL_MAX.get(spec.unit)
    return ceiling is None or amount <= ceiling


# Fat and protein always carry their calories — 9 and 4 kcal per gram — with no
# calorie-free exception. Only carbohydrate has one (sugar alcohols, which a
# label may count as carbs but not energy), so we deliberately leave carbs out.
# A food's stated energy therefore cannot fall far below what its fat and protein
# alone must contribute; when it does, the two figures physically contradict each
# other and one of them is wrong:
#
#     Nutella       0 kcal /  30.9 g fat   (fat alone is ~278 kcal)
#     Mayonnaise    5 kcal /  16.0 g fat
#     Milk chocolate 58 kcal / 36.0 g fat
#
# Each of those values is individually possible — the physical-maximum guard
# passes every one — so the error is visible only across them. This is the same
# guard as is_physically_possible, lifted from a single value to a relationship.
#
# It is aimed at the crowdsourced OFF corpus, whose 2.2M single-source products
# have nothing to correct them, but it holds for any source and catches genuine
# FDC errors too (chocolate filed at 58 kcal). Measured on the local copies it
# drops energy from ~0.6% of OFF-only products and ~0.1% of FDC — and most of
# even the FDC hits are real upstream mistakes, not good data. It cannot fire on
# a legitimate near-zero food (a diet drink, black coffee): with almost no fat or
# protein its floor is near zero, so its energy is never judged.
#
# The threshold is deliberately loose — a quarter of the floor — so only gross
# contradictions flag and label rounding or drained-oil quirks are left alone.
_ENERGY_FLOOR_MIN_KCAL = 15.0
_ENERGY_FLOOR_FRACTION = 0.25


# A component nutrient cannot exceed the whole it is part of. Saturated and trans
# fat are each a fraction of total fat; sugars are a fraction of carbohydrate; and
# added sugars are a fraction of total sugars. A child above its parent is a
# definitional impossibility — and, like the energy floor, invisible to the
# per-value guard because each number is individually fine.
#
# These hold in every labelling convention. Fibre deliberately does not appear:
# US labels count it inside carbohydrate, EU labels report it separately, so
# "fibre exceeds carbohydrate" is normal for the European-sourced OFF data, not
# an error — comparing them would discard real values. Sugars-in-carbohydrate is
# safe either way, since sugars are available carbohydrate under both conventions.
#
# When a child exceeds its parent the child is dropped: the total (fat,
# carbohydrate, sugars) is the headline figure a label leads with and the more
# reliable one, while the breakdown line beneath it is more often mistyped.
# Measured on the local copies this drops a subset value from ~3.3% of OFF-only
# products — almost all of them added sugars over total sugars — and ~0.4% of FDC.
#
# Outermost pair first, so a parent dropped for its own violation is already gone
# when its children are checked against it.
_NUTRIENT_SUBSETS = (
    ("sugars", "carbohydrates"),
    ("added_sugars", "sugars"),
    ("saturated_fat", "fat"),
    ("trans_fat", "fat"),
)
_SUBSET_TOLERANCE = 1.02   # 2% slack for rounding on either side


def _enforce_subsets(values: dict[str, float]) -> None:
    """Drop a component nutrient that exceeds the whole it belongs to."""
    for child, parent in _NUTRIENT_SUBSETS:
        c = values.get(child)
        p = values.get(parent)
        if c is not None and p is not None and c > p * _SUBSET_TOLERANCE + 0.1:
            del values[child]


def _reconcile_energy(values: dict[str, float]) -> None:
    """Drop a stated energy that physically contradicts fat and protein.

    Mutates `values`, removing `calories_kcal` when it sits far below the minimum
    the fat and protein must contribute. The macros set the floor and are kept;
    the energy is the value provably beneath it, and a zeroed or per-serving
    energy is the usual culprit. A nutrient we drop is reported as absent, which
    is honester than a number we can prove is impossible.
    """
    kcal = values.get("calories_kcal")
    if kcal is None:
        return
    floor = 4.0 * values.get("protein", 0.0) + 9.0 * values.get("fat", 0.0)
    if floor >= _ENERGY_FLOOR_MIN_KCAL and kcal < floor * _ENERGY_FLOOR_FRACTION:
        del values["calories_kcal"]


def _unit_matches(entry: dict, fdc_id: int) -> bool:
    """Is this entry denominated in the unit we expect for its id?

    An entry that declares no unit is accepted — we cannot check what we were
    not told, and rejecting it would throw away good data. An entry that
    declares a *contradictory* unit is rejected.
    """
    declared = entry.get("unit")
    if not declared:
        return True
    declared = str(declared).strip().translate(_MICRO_SIGNS).upper()
    declared = _UNIT_ALIASES.get(declared, declared)
    return declared == _FDC_UNIT.get(fdc_id, declared)


NUTRIENTS: tuple[NutrientSpec, ...] = (
    # Macros
    NutrientSpec("calories_kcal", (1008, 2047, 2048), "energy-kcal_100g", "kcal"),
    NutrientSpec("protein", (1003,), "proteins_100g", "g"),
    NutrientSpec("fat", (1004, 1085), "fat_100g", "g"),
    NutrientSpec("saturated_fat", (1258,), "saturated-fat_100g", "g"),
    NutrientSpec("trans_fat", (1257,), "trans-fat_100g", "g"),
    NutrientSpec("cholesterol", (1253,), "cholesterol_100g", "mg"),
    NutrientSpec("carbohydrates", (1005,), "carbohydrates_100g", "g"),
    NutrientSpec("fiber", (1079, 2033), "fiber_100g", "g"),
    NutrientSpec("sugars", (2000, 1063), "sugars_100g", "g"),
    NutrientSpec("added_sugars", (1235,), "added-sugars_100g", "g"),
    # Micronutrients required on a US Nutrition Facts label
    NutrientSpec("sodium", (1093,), "sodium_100g", "mg"),
    NutrientSpec("potassium", (1092,), "potassium_100g", "mg"),
    NutrientSpec("calcium", (1087,), "calcium_100g", "mg"),
    NutrientSpec("iron", (1089,), "iron_100g", "mg"),
    NutrientSpec("vitamin_d", (1114, 1110), "vitamin-d_100g", "µg"),

    # Vitamins beyond the mandatory label panel. FDC id, OFF key, and unit for
    # every one of these were independently verified against real live FDC and
    # Open Food Facts payloads this round (see the module docstring) — not
    # taken on nutrimetrics' word alone.
    NutrientSpec("vitamin_a", (1106, 1104), "vitamin-a_100g", "µg"),
    NutrientSpec("vitamin_c", (1162,), "vitamin-c_100g", "mg"),
    NutrientSpec("vitamin_e", (1109,), "vitamin-e_100g", "mg"),
    NutrientSpec("vitamin_k", (1185,), "vitamin-k_100g", "µg"),
    NutrientSpec("thiamin", (1165,), "vitamin-b1_100g", "mg"),
    NutrientSpec("riboflavin", (1166,), "vitamin-b2_100g", "mg"),
    NutrientSpec("niacin", (1167,), "vitamin-pp_100g", "mg"),
    NutrientSpec("vitamin_b6", (1175,), "vitamin-b6_100g", "mg"),
    NutrientSpec("folate", (1177,), "vitamin-b9_100g", "µg"),
    NutrientSpec("vitamin_b12", (1178,), "vitamin-b12_100g", "µg"),
    NutrientSpec("pantothenic_acid", (1170,), "pantothenic-acid_100g", "mg"),
    NutrientSpec("biotin", (1176,), "biotin_100g", "µg"),
    NutrientSpec("choline", (1180,), "choline_100g", "mg"),

    # Minerals beyond the mandatory label panel.
    NutrientSpec("magnesium", (1090,), "magnesium_100g", "mg"),
    NutrientSpec("zinc", (1095,), "zinc_100g", "mg"),
    NutrientSpec("phosphorus", (1091,), "phosphorus_100g", "mg"),
    NutrientSpec("selenium", (1103,), "selenium_100g", "µg"),
    # 1098 is milligrams on the wire, not micrograms — nutrimetrics' own
    # `display_unit` says µg, which is wrong for what FDC actually returns
    # (confirmed against a live FDC Foundation Foods payload). Copper's own
    # Daily Value is commonly quoted in µg (900 µg), which is presumably how
    # that mismatch happened; the two are easy to conflate by eye.
    NutrientSpec("copper", (1098,), "copper_100g", "mg"),
    NutrientSpec("manganese", (1101,), "manganese_100g", "mg"),
    # Molybdenum's OFF key was not directly observed in a live sample (it is
    # among the most sparsely-analyzed nutrients in both FDC and OFF), but
    # follows the same `{element}_100g` pattern confirmed for every other
    # mineral above.
    NutrientSpec("molybdenum", (1102,), "molybdenum_100g", "µg"),

    NutrientSpec("caffeine", (1057,), "caffeine_100g", "mg"),
)

_BY_FIELD: dict[str, NutrientSpec] = {spec.field: spec for spec in NUTRIENTS}

# Units Open Food Facts publishes in, where they differ from ours. OFF reports
# every one of these in grams per 100 g, including the ones a label shows in
# milligrams — so sodium arrives as 0.0428 g, not 42.8 mg.
_OFF_GRAMS_TO_MG = {
    "sodium", "potassium", "calcium", "iron", "cholesterol",
    "vitamin_c", "vitamin_e", "thiamin", "riboflavin", "niacin", "vitamin_b6",
    "pantothenic_acid", "choline", "magnesium", "zinc", "phosphorus",
    "copper", "manganese", "caffeine",
}
_OFF_GRAMS_TO_UG = {
    "vitamin_d", "vitamin_a", "vitamin_k", "folate", "vitamin_b12", "biotin",
    "selenium", "molybdenum",
}


def from_usda(nutrients: list[dict]) -> dict[str, float]:
    """Pull our nutrients out of an FDC food's nutrient list, by id.

    `nutrients` is a list — not a dict keyed by name — precisely so that the two
    "Energy" entries cannot collapse into one another.
    """
    by_id: dict[int, dict] = {}
    for entry in nutrients:
        if isinstance(entry, dict) and entry.get("id") is not None:
            by_id.setdefault(entry["id"], entry)

    values: dict[str, float] = {}
    for spec in NUTRIENTS:
        for fdc_id in spec.fdc_ids:
            entry = by_id.get(fdc_id)
            if not entry or entry.get("amount") is None:
                continue
            if not _unit_matches(entry, fdc_id):
                continue
            try:
                amount = float(entry["amount"])
            except (TypeError, ValueError):
                continue
            # Convert into the unit we publish. Vitamin D's 1110 is in IU, not
            # micrograms, and taking it at face value inflates it 40-fold.
            scale = _FDC_SCALE.get(fdc_id)
            if scale is not None:
                amount = round(amount * scale, 4)
            if not is_physically_possible(spec.field, amount):
                continue
            values[spec.field] = amount
            break

    # Energy only in kilojoules: convert rather than drop it, but never mistake
    # it for kilocalories.
    if "calories_kcal" not in values:
        kj = by_id.get(ENERGY_KJ_ID)
        if kj and kj.get("amount") is not None and _unit_matches(kj, ENERGY_KJ_ID):
            try:
                values["calories_kcal"] = round(float(kj["amount"]) / KJ_PER_KCAL, 1)
            except (TypeError, ValueError):
                pass

    _enforce_subsets(values)
    _reconcile_energy(values)
    return values


def from_off(nutrients_per_100g: dict) -> dict[str, float]:
    """Pull our nutrients out of an Open Food Facts per-100g payload.

    OFF publishes every nutrient in grams, including those a label shows in
    milligrams or micrograms — sodium comes back as 0.0428, not 42.8 — so the
    ones we publish in mg/µg are converted here rather than served a thousand
    times too small.
    """
    values: dict[str, float] = {}
    for spec in NUTRIENTS:
        raw = nutrients_per_100g.get(spec.field)
        if raw is None:
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            continue
        if spec.field in _OFF_GRAMS_TO_MG:
            amount *= 1000.0
        elif spec.field in _OFF_GRAMS_TO_UG:
            amount *= 1_000_000.0
        if not is_physically_possible(spec.field, amount):
            continue
        values[spec.field] = amount
    _enforce_subsets(values)
    _reconcile_energy(values)
    return values


# The OFF payload keys we ask for, so the wrapper requests exactly what we use.
OFF_KEYS: tuple[str, ...] = tuple(spec.off_key for spec in NUTRIENTS)


def to_usda_entries(values: dict[str, float | None]) -> list[dict]:
    """Turn mapped nutrient values back into an FDC-shaped nutrient list.

    The local bulk database stores nutrients as columns, already resolved to our
    ids and units at import time. Rendering them back into the list shape FDC
    returns means the local tier and the live API hand the orchestrator the same
    thing, and `from_usda` stays the single place that decides what a nutrient
    is — rather than a second, quietly diverging copy of the mapping.
    """
    entries: list[dict] = []
    for spec in NUTRIENTS:
        amount = values.get(spec.field)
        if amount is None:
            continue
        entries.append({
            "id": spec.fdc_ids[0],
            "name": spec.field,
            "amount": amount,
            "unit": _FDC_UNIT.get(spec.fdc_ids[0]),
        })
    return entries
