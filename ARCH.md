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

### 3. Thread Isolation Between Upstreams

Both vendor SDKs (`usda-fdc`, `openfoodfacts`) are synchronous, so every call occupies a thread for its entire duration. Each source therefore gets its **own bounded thread pool** (`UPSTREAM_MAX_THREADS`, default 8) rather than sharing asyncio's default executor.

This is not a micro-optimization. On the shared pool a stalled upstream holds every thread, and a perfectly healthy *other* upstream then times out waiting for one — a single sick vendor takes the whole service down. The circuit breakers cannot prevent it, because the contention is underneath them; and `asyncio.wait_for` cancels only the await, never the blocking call itself, so a stalled thread stays occupied until the SDK's own socket gives up. Dedicated pools contain the damage to the source that caused it. Threads are named `upstream-off` / `upstream-usda` so a thread dump during an incident says which vendor is stuck.

### 4. Fault Tolerance & Resiliency
External boundaries are protected by tight timeout windows (2.0s per upstream call, via `asyncio.wait_for`) and per-source **circuit breakers** (`app/core/resilience.py`): after 5 consecutive failures a source is skipped entirely for a 60s cooldown, then probed half-open. When an upstream dependency fails or drops connections, the error is isolated. The `DataOrchestrator` converts the partial response into the standardized contract, noting the contributing sources inside the metadata payload (`data_sources`, `upstream_latency_ms`) while ensuring high platform availability (200 OK statuses with partial contents over systematic downtime).
