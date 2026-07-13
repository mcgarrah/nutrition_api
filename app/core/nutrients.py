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
listing only the kcal energy ids and never 1062.

The set below is the complete **US Nutrition Facts panel** — what a consumer
expects to find on a label — rather than an arbitrary subset.

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
)

# Units Open Food Facts publishes in, where they differ from ours. OFF reports
# every one of these in grams per 100 g, including the ones a label shows in
# milligrams — so sodium arrives as 0.0428 g, not 42.8 mg.
_OFF_GRAMS_TO_MG = {"sodium", "potassium", "calcium", "iron", "cholesterol"}
_OFF_GRAMS_TO_UG = {"vitamin_d"}


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
            if entry and entry.get("amount") is not None:
                values[spec.field] = entry["amount"]
                break

    # Energy only in kilojoules: convert rather than drop it, but never mistake
    # it for kilocalories.
    if "calories_kcal" not in values:
        kj = by_id.get(ENERGY_KJ_ID)
        if kj and kj.get("amount") is not None:
            try:
                values["calories_kcal"] = round(float(kj["amount"]) / KJ_PER_KCAL, 1)
            except (TypeError, ValueError):
                pass

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
        values[spec.field] = amount
    return values


# The OFF payload keys we ask for, so the wrapper requests exactly what we use.
OFF_KEYS: tuple[str, ...] = tuple(spec.off_key for spec in NUTRIENTS)
