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

### 0. `usda-fdc` release history (we pin >= 0.2.0)

- **0.1.10** — `gtin_upc` on the models (our fix); request timeout + `FdcTimeoutError`.
- **0.1.11** — **security**: the API key travelled in the query string, and
  `requests` embeds the full URL in its exception text, so the first network
  hiccup wrote the real key into any log that records exceptions. *We log
  exceptions.* Now sent as an `X-Api-Key` header and redacted. Also fixed
  kJ-being-served-as-kcal in its own analysis layer — the same defect we found
  independently downstream.
- **0.2.0** — 404/403/400 now raise `FdcResourceNotFoundError` / `FdcAuthError` /
  `FdcValidationError`, all still deriving from `FdcApiError`; exceptions carry
  `status_code`.

The 0.2.0 breaking changes do not touch us (we catch broadly and never use the
DRI/analysis layer), but the new exception types fixed a real bug on our side —
see the commit for `claude/usda-fdc-0.2.0`: a missing food and a rate limit were
both being counted as *upstream failures*, so five lookups of absent foods in a
row would trip the circuit breaker and shut USDA out for everyone.

### 1. ~~`usda_fdc` drops `gtinUpc`~~ — RESOLVED

Fixed upstream in
[usda_fdc_python#8](https://github.com/mcgarrah/usda_fdc_python/pull/8) and
released as **usda-fdc 0.1.10**, which exposes `gtin_upc` on `Food` and
`SearchResultFood` — and also adds a request `timeout`, since `requests` has no
default and a stalled FDC socket used to block its thread forever.

This repo now pins `usda-fdc>=0.1.10`, verifies the barcode through the public
`client.search()`, and has **deleted** the `_make_request` workaround. It also
passes `timeout=UPSTREAM_TIMEOUT_S` to the client, which is what finally
releases a thread stuck on a stalled socket — `asyncio.wait_for` never could.

That repo also had no CI at all and published to PyPI without running its
tests; both are fixed
([usda_fdc_python#9](https://github.com/mcgarrah/usda_fdc_python/pull/9)).

### 2. ~~We import `gpcc._crawlers`, a private module~~ — RESOLVED

I was wrong about this one: it was never a `gpcc` problem. `gpcc` already
re-exports `get_language` and `get_publications` from its package root and
lists both in `__all__` — they are the *same function objects*. Reaching into
`gpcc._crawlers` bought nothing and risked a private module being renamed under
us in a patch release, which would silently stop the taxonomy auto-updating.

`scripts/import_gpc_xml.py` now imports them from `gpcc` directly. No upstream
change was needed.

While fixing it: `gpcc` was only a *transitive* dependency (via `gs1-gpc`)
despite being imported directly, so it is now declared in `requirements.txt`.
A direct import deserves a direct dependency.

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
