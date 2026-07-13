# Working Notes

Scratch context for this project: things that aren't obvious from the code, and
decisions worth revisiting. Not a spec — see [SPECS.md](SPECS.md) and
[ARCH.md](ARCH.md) for those.

## Sibling libraries under my control

Several of this project's dependencies are my own packages under
[github.com/mcgarrah](https://github.com/mcgarrah). They are **not** third-party
black boxes: if the right fix belongs upstream, change it there rather than
working around it here.

All of these are cloned into `/opt` on the `nutrition-api-dev` LXC, so they can
be reviewed and modified as part of this work.

| Package | Repo | Role in this project |
| :--- | :--- | :--- |
| `usda-fdc` | [usda_fdc_python](https://github.com/mcgarrah/usda_fdc_python) | USDA FoodData Central client |
| `gs1-gpc` | [gs1_gpc_python](https://github.com/mcgarrah/gs1_gpc_python) | GPC XML downloader / parser |
| `gpcc` | [gpcc](https://github.com/mcgarrah/gpcc) | GPC Browser crawler; pulled in *transitively* by `gs1-gpc`, but `scripts/import_gpc_xml.py` also imports it directly for the GS1 version check |
| `oneworldsync` | [oneworldsync_python](https://github.com/mcgarrah/oneworldsync_python) | GS1 GTIN→GPC mapping. Not wired in — the data source is cost-prohibitive |
| `nutrimetrics` | [nutrimetrics](https://github.com/mcgarrah/nutrimetrics) | Nutrient analysis (60+ nutrients, meal plans). Not wired in; see "Ideas" below |

### Prior art

- [shiny-shop](https://github.com/mcgarrah/shiny-shop) — Django/DRF GPC API this project supersedes
- [food_service_nutrition](https://github.com/mcgarrah/food_service_nutrition) — earlier Django GPC prototype

## Known upstream issues

### 1. `usda_fdc` drops `gtinUpc` — highest-value upstream fix

Its food and search models don't expose the barcode field at all, even though
the raw FDC API returns it.

This matters because FDC has no barcode-lookup endpoint: `search_by_upc` queries
a *full-text* search, which happily returns unrelated products for an unknown
barcode (querying `00000000` returned a food whose real barcode was
`0099447210127`). Verifying the match is the only thing standing between us and
serving one product's nutrition under another product's barcode.

Since the model drops the field, `app/core/usda_fdc.py:search_by_upc` reaches
past the library's public API and reads the raw search payload via
`client._make_request`.

**Fix is in flight upstream:**
[usda_fdc_python#8](https://github.com/mcgarrah/usda_fdc_python/pull/8) adds
`gtin_upc` to both models (v0.1.10).

**This repo is blocked on a release, not on the code.** CI installs `usda-fdc`
from PyPI, where the latest is 0.1.9 and has no `gtin_upc`. So the cleanup here
must wait until 0.1.10 is published; only then can we bump the pin, switch
`search_by_upc` to the public `client.search()`, and delete the `_make_request`
workaround. Doing it sooner just breaks CI.

### 2. We import `gpcc._crawlers`, a private module

`scripts/import_gpc_xml.py` does `from gpcc._crawlers import get_language,
get_publications` to ask GS1 for the latest publication version. Same fragility
class as the `usda_fdc` workaround above: a private API of a package I control.
`gpcc` could expose a supported "latest publication version" call, and the
importer could stop reaching into an underscore module.

## Ideas not yet acted on

- **Nutrient coverage.** `CanonicalProduct` carries 7 nutrients. `nutrimetrics`
  models 60+. If the canonical model ever needs to grow past macros, that's the
  natural source rather than hand-rolling more fields.
- **Real GTIN→GPC classification.** Worth being honest that we don't have one.
  `gpcc`'s own README says the classification "cannot be inferred from the
  product barcode or even packaging" — companies assign it internally. Our
  Layer-3 mapping is therefore a *heuristic*: we match Open Food Facts' informal
  category tags against GPC brick descriptions. `oneworldsync` is the only real
  source and its data is cost-prohibitive. This limitation should stay documented
  rather than quietly presented as a true GS1 classification.
