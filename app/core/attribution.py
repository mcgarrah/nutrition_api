"""
Source attribution and licensing.

This is a licence condition, not a courtesy. The Open Food Facts database is
published under the **Open Database License (ODbL 1.0)**, and its product images
under **CC BY-SA 3.0**. Both require attribution, and ODbL adds a share-alike
obligation on derived databases. This service redistributes OFF-derived
product names, brands, ingredient text, allergens, labels, and image URLs on
every lookup, so it owes that attribution — an API that serves the data
publicly without it is simply out of compliance.

USDA FoodData Central is a work of the U.S. federal government and therefore in
the public domain; attribution is customary rather than required. GS1 publishes
the Global Product Classification for open use, and asks that the standard be
credited.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""

# Keyed by the identifiers used in CanonicalProduct.data_sources.
SOURCE_ATTRIBUTION: dict[str, dict[str, str]] = {
    "OpenFoodFacts": {
        "name": "Open Food Facts",
        "url": "https://world.openfoodfacts.org/",
        "license": "ODbL 1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1.0/",
        "notes": (
            "Database under the Open Database License (ODbL) 1.0; contents under "
            "the Database Contents License. Product images are licensed CC BY-SA "
            "3.0. Attribution is required, and derived databases are share-alike."
        ),
    },
    "USDA_FDC": {
        "name": "USDA FoodData Central",
        "url": "https://fdc.nal.usda.gov/",
        "license": "Public domain (U.S. Government work)",
        "license_url": "https://fdc.nal.usda.gov/faq.html",
        "notes": (
            "Produced by the U.S. Department of Agriculture and not subject to "
            "copyright. Attribution is customary rather than required."
        ),
    },
    "GS1_GPC": {
        "name": "GS1 Global Product Classification",
        "url": "https://www.gs1.org/standards/gpc",
        "license": "GS1 standard, free to use",
        "license_url": "https://www.gs1.org/standards/gpc",
        "notes": "Taxonomy published by GS1 for open use.",
    },
}


def for_sources(sources: list[str]) -> dict[str, dict[str, str]]:
    """Attribution for exactly the sources that contributed to a response.

    Only the sources actually used are credited: attributing Open Food Facts on
    a response it had no part in would be as wrong as not attributing it on one
    it did.
    """
    return {
        source: SOURCE_ATTRIBUTION[source]
        for source in sources
        if source in SOURCE_ATTRIBUTION
    }
