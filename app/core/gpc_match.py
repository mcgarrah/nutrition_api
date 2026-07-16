"""
Matching a product to the GS1 GPC taxonomy: a curated table for FDC, a
best-effort fuzzy matcher for Open Food Facts, and nothing pretending to be
more confident than it is.

Read this before touching FDC_CATEGORY_TO_BRICK — the mapping was hand-built
against the real April 2026 GPC taxonomy and the real FDC branded corpus, and
every entry has a reason. See ARCH.md, "GPC Category Matching", for the full
investigation this module is the output of: three separate causes of the old
OFF matcher's ~69% false-positive rate, why FDC's own category was never even
consulted, and why pure text matching against GPC's ~730-word brick vocabulary
has a precision ceiling no amount of tuning removes.

Why FDC gets a curated table and OFF does not
-----------------------------------------------
FDC's `branded_food_category` is a closed, controlled vocabulary: 350 distinct
values, GDSN-standardised, stable across the corpus (100% of FDC branded foods
carry one). That is small enough to hand-verify and stable enough for a
verified mapping to stay correct. Open Food Facts' category tags are millions
of free-text, multi-language, self-reported strings — there is no table to
curate; the sensible response there is a smarter fuzzy matcher (see
orchestrator._fetch_gpc_categories), not a lookup table pretending to be one.

The 350 FDC categories are Pareto-distributed — the top 20 alone cover 48.7%
of all branded foods, the top 90 cover 90.7% — so a curated table does not
need to be exhaustive to be worth having. This one covers roughly the top 50,
chosen deliberately over the next 40: a GPC brick was only added when the
match was genuinely confident, not merely plausible. Categories with no clean
GPC equivalent (see "Deliberately excluded" below) are left out rather than
forced onto the nearest approximate brick — a wrong curated entry is worse
than an honest miss, since "fdc_curated" is meant to mean *verified*.

One structural limitation carries through regardless: GPC splits most food
bricks by physical state (Frozen / Perishable / Shelf Stable), a dimension
FDC's category string does not carry at all. Each entry below picks the state
that is actually the common case for that category in a US retail/branded
context (packaged cookies -> Shelf Stable, refrigerated cheese -> Perishable,
frozen pizza -> Frozen) — this is a considered default, not a guess, but it
means the *state* qualifier on the matched brick can occasionally be wrong even
when the *category* is exactly right. That is a materially smaller error than
today's fuzzy matcher choosing an unrelated category outright.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""

# FDC branded_food_category (exact string, as FDC publishes it) -> GPC
# brick_code. Verified against the real April 2026 GPC taxonomy — every code
# here was looked up in data/gpc.sqlite3 and read, not guessed from memory.
FDC_CATEGORY_TO_BRICK: dict[str, str] = {
    # -- Snacks & confectionery --
    "Candy": "10000047",  # Sugar Candy/Sugar Candy Substitutes Confectionery
    "Chocolate": "10000045",  # Chocolate and Chocolate/Sugar Candy Combinations - Confectionery
    "Chewing Gum & Mints": "10006390",  # Chewing Gum
    # Snacks Other -- FDC combines what GPC keeps as 3 separate bricks
    # (popcorn, nuts, seeds); no single brick fits the combination.
    "Popcorn, Peanuts, Seeds & Related Snacks": "10007276",
    "Chips, Pretzels & Snacks": "10000177",  # Chips/Crisps/Snack Mixes - Natural/Extruded (SS)
    "Other Snacks": "10007276",  # Snacks Other
    "Wholesome Snacks": "10007276",  # Snacks Other -- a marketing label, not a distinct category
    "Snacks": "10007276",  # Snacks Other
    "Snack, Energy & Granola Bars": "10000287",  # Cereal/Muesli Bars

    # -- Bakery --
    "Cookies & Biscuits": "10000161",  # Biscuits/Cookies (Shelf Stable)
    "Breads & Buns": "10000165",  # Bread (Shelf Stable)
    "Cakes, Cupcakes, Snack Cakes": "10000172",  # Cakes - Sweet (Shelf Stable)
    "Croissants, Sweet Rolls, Muffins & Other Pastries": "10000247",  # Pies/Pastries - Sweet
    "Cake, Cookie & Cupcake Mixes": "10000156",  # Baking/Cooking Mixes -- a MIX, not the baked good
    # Pies/Pastries/Pizzas/Quiches - Savoury (Frozen); most branded pizza is frozen.
    "Pizza": "10000248",

    # -- Dairy --
    "Cheese": "10000028",  # Cheese (Perishable)
    "Yogurt": "10000278",  # Yogurt (Perishable)
    "Milk": "10000025",  # Milk (Perishable)
    "Milk/Milk Substitutes": "10006971",  # Milk Substitutes -- kept distinct from plain "Milk"
    "Ice Cream & Frozen Yogurt": "10000215",  # Ice Cream/Ice Novelties (Frozen)

    # -- Condiments, sauces, spreads --
    "Pickles, Olives, Peppers & Relishes": "10000180",  # Chutneys/Relishes (Shelf Stable)
    "Dips & Salsa": "10000200",  # Dressings/Dips (Shelf Stable)
    "Salad Dressing & Mayonnaise": "10006319",  # Mayonnaise/Mayonnaise Substitutes (SS)
    "Ketchup, Mustard, BBQ & Cheese Sauce": "10000280",  # Other Sauces Dipping/Condiments/...
    "Jam, Jelly & Fruit Spreads": "10000217",  # Jams/Marmalades (Shelf Stable)
    "Honey": "10000213",  # Honey (Shelf Stable)
    "Syrups & Molasses": "10000044",  # Syrup/Treacle/Molasses (Shelf Stable)
    "Granulated, Brown & Powdered Sugar": "10000043",  # Sugar/Sugar Substitutes (Shelf Stable)
    "Baking Decorations & Dessert Toppings": "10000195",  # Dessert Sauces/Toppings/Fillings
    "Herbs & Spices": "10000049",  # Herbs/Spices (Shelf Stable)
    "Flours & Corn Meal": "10000203",  # Flour - Cereal/Pulse (Shelf Stable)
    "Vegetable & Cooking Oils": "10000040",  # Oils Edible - Vegetable or Plant (Shelf Stable)

    # -- Beverages --
    # Fruit Juice - Ready to Drink (Shelf Stable)
    "Fruit & Vegetable Juice, Nectars & Fruit Drinks": "10000220",
    "Water": "10000232",  # Packaged Water - Unflavoured
    "Iced & Bottle Tea": "10000313",  # Fruit Herbal Infusions/Tisanes - Liquid/Ready to Drink

    # -- Grains, pasta, cereal --
    "Cereal": "10000211",  # Grains/Cereal - Not Ready to Eat - (Shelf Stable)
    "Pasta by Shape & Type": "10000242",  # Pasta/Noodles - Not Ready to Eat (Shelf Stable)

    # -- Prepared / canned / frozen --
    "Frozen Dinners & Entrees": "10006748",  # Ready-Made Combination Meals - Not RTE (Frozen)
    "Frozen Vegetables": "10000270",  # Vegetables - Prepared/Processed (Frozen)
    "Canned Vegetables": "10000272",  # Vegetables - Prepared/Processed (Shelf Stable)
    # No separate canned-bean brick in GPC; shares the vegetables bucket.
    "Canned & Bottled Beans": "10000272",
    "Canned Fruit": "10000206",  # Fruit - Prepared/Processed (Shelf Stable)
    # Branded/UPC "Tomatoes" in a US retail dataset is almost always canned.
    "Tomatoes": "10000272",
    "Frozen Fish & Seafood": "10000626",  # Aquatic Invertebrates/Fish/Shellfish/... (Frozen)
    "Other Soups": "10000262",  # Soups - Prepared (Shelf Stable)

    # -- Meat --
    "Sausages, Hotdogs & Brats": "10005836",  # Mixed Species Sausages - Prepared/Processed
    # Pork - Prepared/Processed: the most common default; FDC's category is
    # not species-specific, but GPC's sausage/cold-cut bricks are.
    "Pepperoni, Salami & Cold Cuts": "10005781",

    # -- Baby --
    # FDC's own data has two inconsistent spellings of this category -- a
    # double space and a hyphenated-single-space variant -- so both are mapped
    # rather than assuming the tidier one is the only one that occurs.
    "Baby/Infant  Foods/Beverages": "10000610",  # Baby/Infant - Foods/Beverages Variety Packs
    "Baby/Infant - Foods/Beverages": "10000610",  # Baby/Infant - Foods/Beverages Variety Packs
}

# Deliberately excluded, and why -- documented so the gap reads as a decision,
# not an oversight, and so nobody re-adds a low-confidence guess later:
#
#   Soda                          GPC has no carbonated-soft-drink/cola brick.
#                                  The nearest bricks (Drinks Flavoured, Stimu-
#                                  lant/Energy Drinks) are different products.
#   Other Drinks, Other Deli,
#   Other Meats, Snacks (dup.)    Genuinely catch-all FDC labels with no single
#                                  correct GPC brick by definition.
#   Nut & Seed Butters            No dedicated GPC brick found (checked the
#                                  Sweet Spreads class, where Honey and Jam
#                                  live -- peanut butter is not there).
#   Prepared Pasta & Pizza
#   Sauces / Oriental, Mexican &
#   Ethnic Sauces                 Would collide with Ketchup/Mustard/BBQ on the
#                                  same "Other Sauces" brick, which already
#                                  covers one FDC category better than three.
#   Rice                          GPC has no standalone rice brick; the closest
#                                  (Grains/Cereal) is used for the "Cereal"
#                                  category above and would be a worse fit here.
#   Pre-Packaged Fruit & Vegetables  Too ambiguous (fresh-cut? mixed? which
#                                  state?) for a confident single brick.


def curated_brick_for_fdc_category(category: str | None) -> str | None:
    """The verified GPC brick for an FDC branded_food_category, if we have one."""
    if not category:
        return None
    return FDC_CATEGORY_TO_BRICK.get(category.strip())


async def hierarchy_for_brick(db, brick_code: str) -> list[str]:
    """Segment > Family > Class > Brick description, for a known brick code.

    Shared by both the curated FDC path and the fuzzy OFF path in
    orchestrator.py, so a hierarchy built from a brick code always has the
    same shape regardless of which source found it.
    """
    rows = await db.execute_fetchall(
        """SELECT b.description, c.description, f.description, s.description
           FROM bricks b
           LEFT JOIN classes c ON b.class_code = c.class_code
           LEFT JOIN families f ON c.family_code = f.family_code
           LEFT JOIN segments s ON f.segment_code = s.segment_code
           WHERE b.brick_code = ?""",
        [brick_code],
    )
    if not rows:
        return []
    brick_desc, cls_desc, fam_desc, seg_desc = rows[0]
    return [p for p in (seg_desc, fam_desc, cls_desc, brick_desc) if p]
