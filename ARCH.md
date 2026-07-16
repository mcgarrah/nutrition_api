# ARCH.md - System Architecture

## Architectural Overview
The Unified Food Intelligence API is an asynchronous, container-native data aggregation gateway. It acts as a performance-optimized orchestration layer between web clients (or ingestion pipelines) and three distinct food telemetry datasets.

```
                  [ Web Client / ML Ingestion Pipeline ]
                                    |
                                    v (HTTP GET /api/v1/lookup/{gtin})
                       +-------------------------+
                       |   FastAPI Gateway API   |
                       +-------------------------+
                                    |
          +-------------------------+-------------------------+
          | (Async Task)            | (Async Task)            | (Local DB Query)
          v                         v                         v
+-------------------+     +-------------------+     +-------------------+
|     USDA FDC      |     |  Open Food Facts  |     |   Local SQLite    |
|   (Remote API)    |     |   (Remote API)    |     |   (GS1 GPC Cache) |
+-------------------+     +-------------------+     +-------------------+
          |                         |                         |
          +-------------------------+-------------------------+
                                    |
                                    v
                       +-------------------------+
                       |    DataOrchestrator     | (Schema Normalization & Ranked Truth)
                       +-------------------------+
                                    |
                                    v (CanonicalProduct JSON Response)
```

## Key Technical Design Components

### 1. Non-Blocking I/O Gateway
The runtime leverages an ASGI architecture powered by **FastAPI** and **Uvicorn**. Instead of blocking execution lines on latent upstream network calls, `asyncio.gather` spawns concurrent I/O operations. The worst-case latency of a single request is capped by the slowest responding service, rather than the cumulative sum of all services. Synchronous upstream SDKs (usda-fdc, openfoodfacts) are bridged into the event loop via thread-pool executors.

### 2. Tiered Storage & Caching Layer
To optimize hosting costs on the **DigitalOcean App Platform** and avoid reliance on heavy infrastructure like Redis, caching is implemented on two levels:
1. **In-Memory LRU/TTL Cache:** `cachetools.TTLCache` caches hot GTIN lookups at the application level (default: 1024 entries, 300s TTL — tunable via `LOOKUP_CACHE_MAX_SIZE` / `LOOKUP_CACHE_TTL_S`) to prevent redundant network round-trips for high-volume items. Only results with at least one contributing source are cached, so transient upstream failures can recover. Entries are keyed on the **GTIN-14 normalized** barcode, so the same product written with different zero-padding shares one entry rather than costing a fresh round trip each way.

   A cached response is marked `cached: true`. Its `upstream_latency_ms` describes the fetch that *produced* the data, not the request that just returned it — without the flag, a 1 ms cache hit would still claim it spent 500 ms querying USDA.

   The cache is **per worker process**. Running `--workers N` gives N independent caches, so each worker warms separately and a repeated GTIN can miss until every worker has seen it. That is the accepted cost of avoiding a shared cache tier (see the Redis note above); with the default two workers it means at most one extra upstream fetch per hot barcode.
2. **Bundled SQLite Database:** Serves as a static read-only cache baked into the Docker image at build time, storing the GS1 Global Product Classification taxonomy (food segments) to enable immediate local lookups without network overhead. At runtime the app can self-update the taxonomy from GS1 when a newer release is published.

The taxonomy import is **atomic and cross-process locked**. Every uvicorn worker runs the startup lifespan, so `--workers N` means N processes reach the importer on the same boot. It builds into a temporary file and `os.replace()`s it into position, and holds an exclusive `flock` for the whole decide-and-import sequence — so a second worker waits, then finds the work already done rather than re-downloading 27 MB and rebuilding the same file underneath the first.

3. **On-Disk Response Store:** every upstream response is kept as an individual JSON record under `data/responses/`, with the payload as it arrived and a UTC timestamp, and served on a repeat lookup within its TTL (default 30 days — food composition is close to static).

   This is load-bearing rather than decorative. The in-memory cache dies with the process, so every deploy re-spent Open Food Facts' entire 15-requests-per-minute allowance re-fetching barcodes already seen; and it is per-worker, so two workers paid twice. A record on disk costs neither a request nor a rate-limit token. It also removes the *search* call from a USDA barcode lookup on the second visit, by remembering which FDC id a barcode resolved to.

   Writes are atomic (temp file + `os.replace`), so nothing ever reads a half-written record, and `scripts/import_store_to_sqlite.py` turns the corpus into a queryable database — the payload preserved verbatim, with the fields worth querying lifted out beside it.

### 3. Thread Isolation Between Upstreams

Both vendor SDKs (`usda-fdc`, `openfoodfacts`) are synchronous, so every call occupies a thread for its entire duration. Each source therefore gets its **own bounded thread pool** (`UPSTREAM_MAX_THREADS`, default 8) rather than sharing asyncio's default executor.

This is not a micro-optimization. On the shared pool a stalled upstream holds every thread, and a perfectly healthy *other* upstream then times out waiting for one — a single sick vendor takes the whole service down. The circuit breakers cannot prevent it, because the contention is underneath them; and `asyncio.wait_for` cancels only the await, never the blocking call itself, so a stalled thread stays occupied until the SDK's own socket gives up. Dedicated pools contain the damage to the source that caused it. Threads are named `upstream-off` / `upstream-usda` so a thread dump during an incident says which vendor is stuck.

### 4. Fault Tolerance & Resiliency
External boundaries are protected by tight timeout windows (2.0s per upstream call, via `asyncio.wait_for`) and per-source **circuit breakers** (`app/core/resilience.py`): after 5 consecutive failures a source is skipped entirely for a 60s cooldown, then probed half-open. When an upstream dependency fails or drops connections, the error is isolated. The `DataOrchestrator` converts the partial response into the standardized contract, noting the contributing sources inside the metadata payload (`data_sources`, `upstream_latency_ms`) while ensuring high platform availability (200 OK statuses with partial contents over systematic downtime).

### 5. GPC Category Matching

Mapping a product to the GS1 GPC taxonomy turned out to be a much harder problem than it looked, and the investigation that led to the current design is worth recording so nobody re-discovers the same failure modes by hand.

**The starting point was worse than it appeared.** The only matcher in the system (`orchestrator._fetch_gpc_categories`) took Open Food Facts' first three category tags and did `bricks.description LIKE '%label%'`, first hit wins. On the real local corpus this matched only **44.9%** of OFF products carrying a category — and a manual audit of the matches showed **69% of them were noise**: a generic single word (`beverages`, `snacks`, `food`) happened to be a literal substring of an unrelated brick's name. The clearest case: the tag `en:beverages` matched the brick *"Alcoholic Beverages Variety Packs"* — because "beverages" is a substring of it — and that single collision misclassified 110,000+ products, including things like plain pasta. Three separable causes, each independently confirmed:

1. **Wrong tag order.** OFF orders category tags broad → narrow (`beverages` → `carbonated-drinks` → `sodas`). The code took the *first* three tags — the broadest, least specific ones — which is exactly backwards.
2. **Substring matching, no word boundaries.** `LIKE '%beverages%'` matches the word anywhere, including inside an unrelated brick name.
3. **Even word-boundary matching has a precision ceiling.** A corrected prototype (in-memory word index, most-specific-tag-first, stopword filtering, "prefer the least common word") pushed recall to ~87% — but a manual spot-check still found real misclassifications from polysemous words (`beans` → coffee beans vs. legumes; `spring` → spring water vs. spring onions) and OFF vocabulary that simply isn't in GPC's ~730-word brick description vocabulary at all (`flageolet`, a bean variety, has no representation anywhere in GPC). **No amount of tuning text matching removes this ceiling** — GPC's ~879 bricks are a coarse, closed vocabulary; OFF's millions of free-text, multi-language tags are not.

**FDC's own category was a separate, larger gap.** `usda_data["category"]` (FDC's `branded_food_category`, 100% populated on every branded food) was **never even consulted** — the matcher only ever looked at OFF's tags, so every FDC-only product got an empty `category_hierarchy` regardless of how good a match its own category would have made.

**The fix treats the two sources differently, because they have different shapes.** FDC's `branded_food_category` is a **closed, controlled vocabulary** — 350 distinct values across the whole corpus, GDSN-standardised, Pareto-distributed (the top 20 alone cover 48.7% of all branded foods; the top 90 cover 90.7%). That is small and stable enough to **hand-verify**, so `app/core/gpc_match.py` carries two curated tables — each entry looked up and read against the real GPC taxonomy, not matched by string similarity — covering **86.0%** of all FDC foods with a category:

- `FDC_CATEGORY_TO_BRICK` (85 entries) — the specific GPC brick for a category, used when one exists that faithfully represents it.
- `FDC_CATEGORY_TO_CLASS` (73 entries) — a coarser GPC class, used when FDC's category is confidently a whole class of products but no single brick fits. The bulk of this table (69 entries) exists because FDC's newer category taxonomy borrows GPC's own **class names verbatim** (modulo whitespace/punctuation noise like `"Fish  Prepared/Processed"` vs. `"Fish - Prepared/Processed"`, both of which occur in the real data and are mapped) — evidence of deliberate vocabulary alignment between the two standards, not a coincidence worth fuzzy-matching around.

`curated_hierarchy_for_fdc_category()` tries the brick table first — the more specific unit — and only falls back to the class table when no brick entry exists for that category; the two tables never both claim the same category key. Categories with no clean GPC equivalent at either level (`Soda` — GPC has no carbonated-drink brick or class at all; `Nut & Seed Butters`; `Frozen Patties and Burgers`; `Chili & Stew`; several "Other X" catch-alls — see the "Deliberately excluded" comment block in `gpc_match.py` for the full, current list) are **deliberately left out** rather than forced onto an approximate match: a wrong curated entry undermines the one property that makes curation worth having.

Open Food Facts' tags have no equivalent closed vocabulary to curate — the sensible response there is a smarter *matcher*, not a lookup table pretending to be one. The original fuzzy `_fetch_gpc_categories` path is retained as the fallback for products without a curated FDC match; its known ~69% noise rate on raw hits is why it is explicitly graded lower than the curated path (see below), not why it was removed.

**The result is exposed as a confidence field, not a single ambiguous list.** `CanonicalProduct.category_hierarchy_source` tells a caller which of the two paths (or neither) produced the answer:

| value | meaning |
| :--- | :--- |
| `fdc_curated` | FDC's category, resolved through the hand-verified table. High confidence. |
| `off_fuzzy` | OFF's tags, resolved by best-effort text matching. Real matches, not verified case by case — a hint, not ground truth. |
| `none` | Neither path produced a GPC classification. `category_hierarchy` may still carry OFF's *raw* tags as a last-resort fallback in this case — those are upstream labels, not a GPC match, which is exactly why the source stays `none` rather than being labelled as a result. |

A future `reviewed` tier is planned for `off_fuzzy` matches that have since been human-checked, once that review workflow exists. Not implemented yet — the field is typed to make adding it additive, not a breaking change.

The curated path is tried first and, on a hit, the fuzzy path is skipped entirely — not run in the background and discarded, actually skipped, so a curated answer never pays for a query it doesn't need.
