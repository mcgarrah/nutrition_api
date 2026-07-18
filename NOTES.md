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

### 1. `nutrimetrics`' copper `display_unit` does not match what FDC returns

Learned from `nutrimetrics` while expanding `app/core/nutrients.py` from the
US label panel to the full vitamin/mineral set: its `Nutrient('copper', ...,
unit_microgram)` declares copper in micrograms. A live FDC payload (id 1098,
checked against a real Foundation Foods entry) actually returns it in
**milligrams**. `display_unit` there is chosen for `nutrimetrics`' own
DRI-comparison workbook — copper's 900 µg RDA is the natural unit to show a
person — not for what FDC puts on the wire, so the two purposes silently
diverged. Not a `nutrimetrics` bug, since it never claims to describe FDC's
transport unit, but a trap for anyone porting its nutrient list assuming it
does. `nutrients.py` publishes copper in mg, verified against the live API,
with the discrepancy documented in a code comment. No upstream change needed.

## Ideas not yet acted on

- **Custom domain (`nutrition-api-dev.mcgarrah.org`) for the public site.**
  See [PLAN.md](PLAN.md) item 1 for the full writeup — shelved until the
  domain finishes migrating from Squarespace to Porkbun. Short version:
  Tailscale Funnel has no custom-domain support (it only issues/serves TLS for
  `*.ts.net`), so a plain CNAME to the Funnel hostname resolves but fails TLS.
  The preferred fix found while researching this (2026-07-17) is Funnel's raw
  `--tcp=443` forwarder plus Caddy doing its own DNS-01 ACME against Porkbun —
  full detail, commands, and the multi-service front-Caddy architecture it
  enables are in PLAN.md, not duplicated here.

- **Real GTIN→GPC classification.** Worth being honest that we don't have one.
  `gpcc`'s own README says the classification "cannot be inferred from the
  product barcode or even packaging" — companies assign it internally. Our
  Layer-3 mapping is therefore a *heuristic*: we match Open Food Facts' informal
  category tags against GPC brick descriptions. `oneworldsync` is the only real
  source and its data is cost-prohibitive. This limitation should stay documented
  rather than quietly presented as a true GS1 classification.
