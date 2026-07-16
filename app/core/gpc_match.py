"""
Matching a product to the GS1 GPC taxonomy: a curated table for FDC, a
best-effort fuzzy matcher for Open Food Facts, and nothing pretending to be
more confident than it is.

Read this before touching FDC_CATEGORY_TO_BRICK or FDC_CATEGORY_TO_CLASS — both
tables were hand-built against the real April 2026 GPC taxonomy and the real
FDC branded corpus, and every entry has a reason. See ARCH.md, "GPC Category
Matching", for the full investigation this module is the output of: three
separate causes of the old OFF matcher's ~69% false-positive rate, why FDC's
own category was never even consulted, and why pure text matching against
GPC's ~730-word brick vocabulary has a precision ceiling no amount of tuning
removes.

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
need to be exhaustive to be worth having. Between them, FDC_CATEGORY_TO_BRICK
(128 entries, a specific brick) and FDC_CATEGORY_TO_CLASS (82 entries, a
coarser class used when no single brick fits — see that table's own docstring)
cover 91.2% of all FDC foods with a category. A GPC brick or class was only
added when the match was genuinely confident, not merely plausible. Categories
with no clean GPC equivalent at either level (see "Deliberately excluded"
below) are left out rather than forced onto the nearest approximate match — a
wrong curated entry is worse than an honest miss, since "fdc_curated" is
meant to mean *verified*.

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
import sqlite3

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

    # -- Second verification pass: 27 more brick-level entries, ranks ~12-130
    # by FDC food count. Same discipline as above -- every code below was
    # looked up in data/gpc.sqlite3, not guessed.
    "Seasoning Mixes, Salts, Marinades & Tenderizers": "10000619",
    "Powdered Drinks": "10000202",
    "Crackers & Biscotti": "10000161",  # shares the Biscuits/Cookies brick
    "Canned Soup": "10000262",
    "Frozen Fruit & Fruit Juice Concentrates": "10000307",
    "Energy, Protein & Muscle Recovery Drinks": "10000266",
    "Other Frozen Desserts": "10000196",
    "Vegetable and Lentil Mixes": "10000272",  # shares the canned-vegetables brick
    "Butter & Spread": "10000169",
    "Bacon, Sausages & Ribs": "10005781",  # shares Pepperoni, Salami & Cold Cuts
    "Mexican Dinner Mixes": "10000156",  # shares Baking/Cooking Mixes
    "Pasta Dinners": "10000241",
    "Tea Bags": "10000119",
    "Entrees, Sides & Small Meals": "10006748",  # shares Frozen Dinners & Entrees
    "Milk Additives": "10008495",
    "Fish & Seafood": "10000017",
    "Canned Seafood": "10000018",
    "Plant Based Milk": "10006971",  # shares Milk/Milk Substitutes
    "Puddings & Custards": "10000312",
    "Frozen Bread & Dough": "10000163",
    "Alcohol": "10000591",
    "Prepared Subs & Sandwiches": "10000255",
    "Bread & Muffin Mixes": "10000156",  # shares Baking/Cooking Mixes
    "Canned Tuna": "10000018",  # shares Canned Seafood
    "Frozen Breakfast Sandwiches, Biscuits & Meals": "10006748",  # shares frozen entrees
    "All Noodles": "10000242",  # shares Pasta by Shape & Type
    "French Fries, Potatoes & Onion Rings": "10000270",  # shares Frozen Vegetables
    "Canned Condensed Soup": "10000262",  # shares Canned Soup / Other Soups
    "Gelatin, Gels, Pectins & Desserts": "10000312",  # shares Puddings & Custards
    "Baking Additives & Extracts": "10006214",
    "Gravy Mix": "10000156",  # shares Baking/Cooking Mixes
    "Liquid Water Enhancer": "10008495",  # shares Milk Additives
    "Frozen Prepared Sides": "10006748",  # shares frozen entrees
    "Sport Drinks": "10000265",
    # FDC's own spelling has a typo ("Burittos", not "Burritos") -- must match
    # the string FDC actually publishes, not the correct English spelling.
    "Prepared Wraps and Burittos": "10000254",
    "Flavored Snack Crackers": "10000161",  # shares Biscuits/Cookies

    # -- Third verification pass. Most of these are FDC borrowing a GPC
    # *brick* description verbatim -- the same vocabulary-alignment pattern
    # that produced most of FDC_CATEGORY_TO_CLASS below, one level deeper.
    # A handful (marked) are manually researched by sampling real
    # foods.description rows under the category, the same discipline as the
    # Baby/Infant and Burittos entries above.
    "Biscuits/Cookies (Shelf Stable)": "10000161",
    "Chips/Crisps/Snack Mixes - Natural/Extruded (Shelf Stable)": "10000177",
    "Cereal/Muesli Bars": "10000287",
    "Fruit - Prepared/Processed (Shelf Stable)": "10000206",
    "Vegetable Based Products / Meals - Not Ready to Eat (Frozen)": "10000291",
    "Cakes - Sweet (Frozen)": "10000170",
    "Pies/Pastries - Sweet (Shelf Stable)": "10000247",
    "Drinks Flavoured - Ready to Drink": "10000201",
    "Ice Cream/Ice Novelties (Shelf Stable)": "10000216",
    "Baking/Cooking Supplies (Shelf Stable)": "10000158",
    "Pies/Pastries/Pizzas/Quiches - Savoury (Frozen)": "10000248",
    "Vegetables - Prepared/Processed (Shelf Stable)": "10000272",
    "Dough Based Products / Meals - Not Ready to Eat - Savoury (Shelf Stable)": "10000302",
    "Baking/Cooking Mixes (Shelf Stable)": "10000156",
    "Popcorn (Shelf Stable)": "10000252",
    "Egg Based Products / Meals - Not Ready to Eat (Frozen)": "10005224",
    "Beer": "10000159",
    "Soups - Prepared (Shelf Stable)": "10000262",
    "Baking/Cooking Mixes (Perishable)": "10000068",
    "Turkey - Unprepared/Unprocessed": "10005803",
    "Pork Sausages - Prepared/Processed": "10005840",
    "Dressings/Dips (Shelf Stable)": "10000200",
    "Sauces - Cooking (Shelf Stable)": "10000057",
    "Pork - Unprepared/Unprocessed": "10005800",
    "Pork - Prepared/Processed": "10005781",
    "Grain Based Products / Meals - Not Ready to Eat - Savoury (Shelf Stable)": "10000297",
    "Flour - Cereal/Pulse (Shelf Stable)": "10000203",
    "Cakes - Sweet (Shelf Stable)": "10000172",
    "Beef - Prepared/Processed": "10005767",
    "Baking/Cooking Mixes/Supplies Variety Packs": "10000595",
    # Meat Substitutes - Non Animal Based (Frozen); sampled products are all
    # meatless/plant-based ("meatless meatballs", "veggie sausage", ...).
    "Vegetarian Frozen Meats": "10005823",
    # Mixed Species Sausages; GPC's sausage bricks carry no Frozen/SS split,
    # so the same brick used for "Sausages, Hotdogs & Brats" fits here too.
    "Frozen Sausages, Hotdogs & Brats": "10005836",
    # Dessert Sauces/Toppings/Fillings (SS); sample is dominated by pie and
    # fruit filling, which is what this brick already covers.
    "Pastry Shells & Fillings": "10000195",
    # Packaged Water - Flavoured; 616/685 sampled products literally contain
    # the word "WATER" (coconut water, flavoured/enhanced water).
    "Plant Based Water": "10008410",
    # Baking/Cooking Mixes; sample is boxed stuffing mix, not raw bread.
    "Stuffing": "10000156",
    # Ready-Made Combination Meals - Not RTE (Perishable) -- refrigerated,
    # not frozen, unlike the already-curated "Frozen Breakfast..." category.
    "Breakfast Sandwiches, Biscuits & Meals": "10006749",
    # Same product as "Breads & Buns" above under FDC's AU/NZ-style naming.
    "Bread - Incl. Buns And Rolls": "10000165",
    "Baking Needs": "10000156",  # sample is brownie/cake/batter mixes
    "Snack Foods - Chips": "10000177",
    "Snack Foods - Other": "10007276",  # sample is jerky/dried-fish snacks
    "Bacon": "10005781",
    # "Smallgoods" is AU/NZ for cured/processed meats -- same product as the
    # existing "Sausages, Hotdogs & Brats" entry.
    "Sausages/Smallgoods": "10005836",
    # Fish - Prepared/Processed (SS); despite the name, sample is 100% fish.
    "Canned Fish and Meat": "10000018",
}

# FDC branded_food_category -> GPC class_code, for categories where FDC's own
# taxonomy borrows a GPC *class* name verbatim (or near-verbatim, modulo
# whitespace/punctuation noise -- see the double-space/hyphen note below).
# This is a coarser match than a brick: a class contains many bricks, so this
# table is used only when we're confident about the category but there is no
# single brick that represents it. Every entry is a class name FDC and GPC
# both use, confirmed by spot-checking real FDC products under the category
# against the class's description, and confirmed to have zero ambiguous
# duplicate class names in the GPC database.
FDC_CATEGORY_TO_CLASS: dict[str, str] = {
    "Aquatic Invertebrates/Fish/Shellfish/Seafood Combination": "50122500",
    "Baking/Cooking Mixes/Supplies": "50181700",
    "Berries/Small Fruit": "50251000",
    "Biscuits/Cookies": "50182100",
    "Bread": "50181900",
    "Bread/Bakery Products Variety Packs": "50182300",
    "Butter/Butter Substitutes": "50131900",
    "Cheese/Cheese Substitutes": "50131800",
    "Chickpeas": "50262200",
    "Coffee/Coffee Substitutes": "50202600",
    "Coffee/Tea/Substitutes": "50201700",
    "Confectionery Products": "50161800",
    "Cream/Cream Substitutes": "50132000",
    "Dairy/Egg Based Products / Meals": "50193500",
    "Dough Based Products / Meals": "50193300",
    "Eggs/Eggs Substitutes": "50132500",
    "Fats Edible": "50151600",
    "Fish  Prepared/Processed": "50121900",  # double space, as FDC publishes it
    "Fish  Unprepared/Unprocessed": "50121500",  # double space
    "Fish - Prepared/Processed": "50121900",
    "Fish Substitutes": "50390200",
    "Fruit  Prepared/Processed": "50102000",  # double space
    "Fruit - Prepared/Processed": "50102000",
    "Fruit/Nuts/Seeds Combination": "50101900",
    "Fruits - Unprepared/Unprocessed (Frozen)": "50270100",
    "Fruits - Unprepared/Unprocessed (Shelf Stable)": "50310100",
    "Fruits/Vegetables/Nuts/Seeds Variety Packs": "50102200",
    "Grain Based Products / Meals": "50193200",
    "Grains/Flour": "50221000",
    "Herbs/Spices/Extracts": "50171500",
    "Meat/Poultry/Other Animals  Prepared/Processed": "50240100",  # double space
    "Meat/Poultry/Other Animals  Unprepared/Unprocessed": "50240200",  # double space
    "Meat/Poultry/Other Animals - Prepared/Processed": "50240100",
    "Meat/Poultry/Other Animals - Unprepared/Unprocessed": "50240200",
    "Meat/Poultry/Other Animals Sausages  Prepared/Processed": "50240300",  # double space
    "Meat/Poultry/Other Animals Sausages - Prepared/Processed": "50240300",
    "Non Alcoholic Beverages  Not Ready to Drink": "50202400",  # double space
    "Non Alcoholic Beverages  Ready to Drink": "50202300",  # double space
    "Non Alcoholic Beverages - Not Ready to Drink": "50202400",
    "Non Alcoholic Beverages - Ready to Drink": "50202300",
    "Nuts/Seeds  Prepared/Processed": "50101800",  # double space
    "Nuts/Seeds - Prepared/Processed": "50101800",
    "Nuts/Seeds - Unprepared/Unprocessed (In Shell)": "50340100",
    "Oils Edible": "50151500",
    "Pasta/Noodles": "50192900",
    "Peppers": "50260400",
    "Pickles/Relishes/Chutneys/Olives": "50171900",
    "Prepared Soups": "50191500",
    "Prepared/Preserved Foods Variety Packs": "50193400",
    "Processed Cereal Products": "50221200",
    "Ready-Made Combination Meals": "50193800",
    "Sandwiches/Filled Rolls/Wraps": "50192500",
    "Sauces/Spreads/Dips/Condiments": "50171800",
    "Savoury Bakery Products": "50182200",
    "Seasonings/Preservatives/Extracts Variety Packs": "50172000",
    "Shellfish Prepared/Processed": "50122100",
    "Shellfish Unprepared/Unprocessed": "50121700",
    "Sugars/Sugar Substitute Products": "50161500",
    "Sweet Bakery Products": "50182000",
    "Sweet Spreads": "50192400",
    "Tea and Infusions/Tisanes": "50202700",
    "Vegetable Based Products / Meals": "50193100",
    "Vegetables  Prepared/Processed": "50102100",  # double space
    "Vegetables  Unprepared/Unprocessed (Frozen)": "50290100",  # double space
    "Vegetables - Prepared/Processed": "50102100",
    "Vegetables - Unprepared/Unprocessed (Frozen)": "50290100",
    "Vegetables - Unprepared/Unprocessed (Shelf Stable)": "50320100",
    "Vinegars/Cooking Wines": "50171700",
    "Yogurt/Yogurt Substitutes": "50132100",

    # Four more FDC categories that are not literal class-name matches but
    # resolve unambiguously to one of the class codes above once read against
    # the real GPC class list -- no brick exists that's specific enough
    # (e.g. GPC has no combined "poultry" brick spanning chicken and turkey).
    "Poultry, Chicken & Turkey": "50240200",  # Meat/Poultry/Other Animals - Unprepared/Unprocessed
    "Canned Meat": "50240100",  # Meat/Poultry/Other Animals - Prepared/Processed
    "Eggs & Egg Substitutes": "50132500",  # Eggs/Eggs Substitutes
    "Cream": "50132000",  # Cream/Cream Substitutes

    # -- Third verification pass --
    # Coffee/Coffee Substitutes; unqualified "Coffee" spans ground, whole-bean,
    # K-cup, and ready-to-drink forms -- no single brick covers all of them.
    "Coffee": "50202600",
    # Same Unprepared/Unprocessed class as "Poultry, Chicken & Turkey" above --
    # the sample mixes raw and seasoned-raw chicken, so a Prepared/Unprepared
    # brick split would misrepresent roughly half the products either way.
    "Frozen Poultry, Chicken & Turkey": "50240200",
    "Confectionery": "50161800",  # Confectionery Products

    # -- Fourth verification pass --
    # These six supersede a round 1/2 exclusion. Rounds 1-2 only checked for a
    # BRICK fit here (correctly finding none -- GPC has no cola/soda brick, no
    # rice brick, no ethnic-sauce brick). This round checked whether the
    # *class* the near-miss brick lives under is itself a clean, non-misleading
    # fit, and verified each one by sampling real foods.description rows --
    # not merely by name resemblance.
    #
    # Non Alcoholic Beverages - Ready to Drink; the sample is 100% carbonated
    # soft drinks, root beer, ginger ale, and energy drinks -- exactly what
    # this class means. (Round 1 correctly found no brick: GPC has no
    # cola/soda-specific brick, only this coarser class.)
    "Soda": "50202300",
    # Grains/Flour; this class already holds the "Cereal" brick, but its
    # class-level scope is grains generally -- confirmed against real
    # products: white/brown/basmati/jasmine rice, no contamination.
    "Rice": "50221000",
    # Same Grains/Flour class; sample is rice-based dinner mixes and pilafs
    # (chicken rice, rice pilaf, jambalaya mix) -- still squarely grain-based.
    "Flavored Rice Dishes": "50221000",
    # Same Grains/Flour class; sample is quinoa, flax, chia, hemp, millet,
    # farro, barley -- genuine grains and seeds, not a catch-all in practice.
    "Other Grains & Seeds": "50221000",
    # Sauces/Spreads/Dips/Condiments; vinegar-contamination check (the reason
    # "Other Cooking Sauces" below stays excluded) came back 1/3,600 = 0.0%
    # for this category -- essentially pure pasta/pizza sauce.
    "Prepared Pasta & Pizza Sauces": "50171800",
    # Same Sauces/Spreads/Dips/Condiments class; vinegar-contamination check
    # came back 12/3,559 = 0.3% -- essentially pure ethnic sauce/paste
    # (teriyaki, soy sauce, curry paste, salsa). Round 2 excluded this for
    # colliding with the "Other Sauces" *brick* already used elsewhere; the
    # class itself has no such collision.
    "Oriental, Mexican & Ethnic Sauces": "50171800",
}

# Deliberately excluded, and why -- documented so the gap reads as a decision,
# not an oversight, and so nobody re-adds a low-confidence guess later. Some
# entries below have since been superseded by a class-level match found in a
# later pass -- see FDC_CATEGORY_TO_CLASS's "Fourth verification pass" comment
# for the ones that moved from here into that table.
#
#   Other Drinks, Other Deli,
#   Other Meats, Snacks (dup.)    Genuinely catch-all FDC labels with no single
#                                  correct GPC brick by definition.
#   Nut & Seed Butters            No dedicated GPC brick *or class* found --
#                                  checked Sweet Spreads (honey/jam bricks
#                                  only) and Sauces/Spreads/Dips/Condiments
#                                  (savoury dressings/mayo/mustard/pate only);
#                                  peanut/nut butter fits neither.
#   Pre-Packaged Fruit & Vegetables  Sample spans whole fresh fruit, fresh-cut
#                                  vegetables, AND prepared salad kits with
#                                  dressing -- three different GPC states
#                                  (Fresh / Fresh Cut / Prepared) in one FDC
#                                  label; no single class covers all three.
#
#   -- second pass additions --
#   Deli Salads, Cooked & Prepared,
#   Lunch Snacks & Combinations    No dedicated brick or class exists for
#                                  either (checked adjacent classes); forcing
#                                  one onto a near neighbour would misrepresent
#                                  the product.
#   Frozen Patties and Burgers    GPC's meat classes/bricks are strictly
#                                  species-specific (Beef, Pork, Chicken, ...),
#                                  and the sample mixes beef, venison, chicken,
#                                  AND plant-based patties -- no single one
#                                  represents the category without the rest
#                                  being wrong.
#   Chili & Stew                  Sample mixes actual chili, pasta-in-sauce
#                                  dishes (beefaroni, spaghetti & meatballs),
#                                  canned diced chiles, and vegetable stew --
#                                  spans classes already used elsewhere
#                                  (Ready-Made Combination Meals, canned
#                                  vegetables); no single fit.
#   Frozen Appetizers &
#   Hors D'oeuvres                Spans too many GPC bricks (pastry, meat,
#                                  seafood, vegetable) to pick one honestly.
#   Frozen Pancakes, Waffles,
#   French Toast & Crepes          Checked Sweet Bakery Products (cakes/pies),
#                                  Savoury Bakery Products (pies/pizza/quiche),
#                                  and Grain Based Products/Meals (savoury
#                                  grain meals) -- none represent pancakes or
#                                  waffles. Confirmed genuine GPC gap.
#
#   -- third pass additions --
#   Specialty Formula Supplements, Meal Replacement Supplements, Weight
#   Control, Digestive & Fiber Supplements, Herbal Supplements, Green
#   Supplements, Children's Nutritional Supplements, Health Care
#                                  Out of scope, not merely uncurated: these
#                                  are dietary supplements, and only the
#                                  Food/Beverage GPC segment is imported (see
#                                  README) -- no brick in it represents a
#                                  supplement without misrepresenting it as an
#                                  ordinary food or beverage.
#   Other Cooking Sauces,
#   Other Condiments              49.5% and 74.7% of these categories
#                                  respectively are literally vinegar by
#                                  description (exact counts, not a sample) --
#                                  contrast the two Sauces/Spreads/Dips/
#                                  Condiments class entries in
#                                  FDC_CATEGORY_TO_CLASS below, which came back
#                                  under 1% vinegar-contaminated. Genuinely
#                                  mixed sauce+vinegar catch-alls; no single
#                                  code fits without misrepresenting half.
#   Frozen Bacon, Sausages & Ribs Sample mixes pork, beef, and turkey in one
#                                  category; GPC's meat bricks are
#                                  species-specific and there is no
#                                  mixed-species "Prepared" brick to use.
#   Other Frozen Meats            Catch-all label, no single correct brick by
#                                  definition (same reasoning as "Other Meats").
#   Pizza Mixes & Other Dry
#   Dinners                       Misleadingly named -- the sample is grain/
#                                  rice/noodle salad mixes, not pizza.
#   Sugar And Flour                Spans two categories already curated
#                                  separately (Granulated/Brown/Powdered Sugar
#                                  and Flours & Corn Meal); no single code
#                                  represents both without misrepresenting half.
#   Dairy Foods/Yoghurts           Small mixed category (yogurt and condensed
#                                  milk together); no single dairy brick
#                                  covers both without misrepresenting half.
#   Sushi                          Data-quality problem, not a mapping one --
#                                  the sample includes non-sushi items (a
#                                  pretzel sandwich roll, a turkey sausage
#                                  patty) alongside real sushi, so the category
#                                  itself looks mistagged in FDC's own data.
#   Crusts & Dough                 Superficially close to the already-curated
#                                  "Dough Based Products / Meals" class, but
#                                  that class's real FDC products are boxed
#                                  macaroni & cheese dinners, not raw dough --
#                                  reusing it here would be wrong. Checked and
#                                  ruled out Biscuits/Cookies (means sweet
#                                  cookies) and Bread (means baked bread) too;
#                                  no brick represents raw refrigerated dough.


def curated_brick_for_fdc_category(category: str | None) -> str | None:
    """The verified GPC brick for an FDC branded_food_category, if we have one."""
    if not category:
        return None
    return FDC_CATEGORY_TO_BRICK.get(category.strip())


def curated_class_for_fdc_category(category: str | None) -> str | None:
    """The verified GPC class for an FDC branded_food_category, if we have one.

    Checked only when there is no brick-level match (see
    curated_hierarchy_for_fdc_category) -- a class is a coarser unit than a
    brick, so brick-level always wins when both exist.
    """
    if not category:
        return None
    return FDC_CATEGORY_TO_CLASS.get(category.strip())


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


async def hierarchy_for_class(db, class_code: str) -> list[str]:
    """Segment > Family > Class description, for a known class code.

    Three levels, not four -- a class-level curated match is confident about
    the category but not about any single brick within it, so there is no
    brick description to add on the end.
    """
    rows = await db.execute_fetchall(
        """SELECT c.description, f.description, s.description
           FROM classes c
           LEFT JOIN families f ON c.family_code = f.family_code
           LEFT JOIN segments s ON f.segment_code = s.segment_code
           WHERE c.class_code = ?""",
        [class_code],
    )
    if not rows:
        return []
    cls_desc, fam_desc, seg_desc = rows[0]
    return [p for p in (seg_desc, fam_desc, cls_desc) if p]


async def curated_hierarchy_for_fdc_category(db, category: str | None) -> list[str]:
    """The best verified hierarchy for an FDC category, or [] if we have none.

    Brick-level is tried first -- it is the more specific unit, so it wins
    whenever both a brick and a class match exist. Class-level is the
    fallback for categories where we're confident about the category but
    there is no single brick that represents it faithfully.
    """
    brick = curated_brick_for_fdc_category(category)
    if brick:
        hierarchy = await hierarchy_for_brick(db, brick)
        if hierarchy:
            return hierarchy

    cls = curated_class_for_fdc_category(category)
    if cls:
        return await hierarchy_for_class(db, cls)

    return []


# ── Bulk hierarchy lookups, for the mapping viewer ─────────────────────
#
# The viewer renders every curated entry at once (currently 85 brick + 73
# class rows) — one query per row would mean over a hundred round trips for a
# single page load. These resolve the whole table in two queries instead.

async def hierarchy_for_bricks(db, brick_codes) -> dict[str, list[str]]:
    """hierarchy_for_brick, batched: {brick_code: [Segment, Family, Class, Brick]}."""
    codes = sorted(set(brick_codes))
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = await db.execute_fetchall(
        f"""SELECT b.brick_code, b.description, c.description, f.description, s.description
            FROM bricks b
            LEFT JOIN classes c ON b.class_code = c.class_code
            LEFT JOIN families f ON c.family_code = f.family_code
            LEFT JOIN segments s ON f.segment_code = s.segment_code
            WHERE b.brick_code IN ({placeholders})""",
        codes,
    )
    return {r[0]: [p for p in (r[4], r[3], r[2], r[1]) if p] for r in rows}


async def hierarchy_for_classes(db, class_codes) -> dict[str, list[str]]:
    """hierarchy_for_class, batched: {class_code: [Segment, Family, Class]}."""
    codes = sorted(set(class_codes))
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = await db.execute_fetchall(
        f"""SELECT c.class_code, c.description, f.description, s.description
            FROM classes c
            LEFT JOIN families f ON c.family_code = f.family_code
            LEFT JOIN segments s ON f.segment_code = s.segment_code
            WHERE c.class_code IN ({placeholders})""",
        codes,
    )
    return {r[0]: [p for p in (r[3], r[2], r[1]) if p] for r in rows}


# ── Coverage report, for the mapping viewer ─────────────────────────────
#
# How much of the real FDC corpus the curated tables actually reach, measured
# against the local bulk copy rather than asserted in a docstring — so the
# number in ARCH.md and the number on screen can never quietly drift apart.

def fdc_category_counts() -> dict[str, int] | None:
    """{branded_food_category: food count}, from the local FDC bulk copy.

    None if the local copy isn't present -- the caller decides how to degrade
    (the mapping viewer shows the curated tables without a coverage number
    rather than failing outright).
    """
    from . import fdc_local
    if not fdc_local.DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{fdc_local.DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM foods "
            "WHERE category IS NOT NULL AND category != '' GROUP BY category"
        ).fetchall()
        return dict(rows)
    finally:
        conn.close()


def coverage_report() -> dict | None:
    """How much of the real FDC corpus the two curated tables reach.

    None if the local FDC copy isn't available to measure against.
    """
    counts = fdc_category_counts()
    if counts is None:
        return None

    total = sum(counts.values())
    covered = 0
    uncovered = []
    for category, count in counts.items():
        if category in FDC_CATEGORY_TO_BRICK or category in FDC_CATEGORY_TO_CLASS:
            covered += count
        else:
            uncovered.append({"category": category, "food_count": count})
    uncovered.sort(key=lambda entry: -entry["food_count"])

    return {
        "total_categorized_foods": total,
        "covered_foods": covered,
        "coverage_pct": round(covered / total * 100, 1) if total else 0.0,
        "distinct_fdc_categories": len(counts),
        "curated_brick_entries": len(FDC_CATEGORY_TO_BRICK),
        "curated_class_entries": len(FDC_CATEGORY_TO_CLASS),
        "uncovered_categories": uncovered,
    }
