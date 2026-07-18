# Plan

Larger pieces of work that are decided in direction but not yet started.
Not a backlog of every idea — see [NOTES.md](NOTES.md) "Ideas not yet acted
on" for those. Items here have enough of a concrete approach that starting
them should mostly be execution, not design.

## Project goals

What this project is *for*, so plan items can be judged against something:

1. **One canonical answer per barcode.** A single `GET /api/v1/lookup/{gtin}`
   that merges USDA FDC (nutrition authority), Open Food Facts (product
   metadata), and GS1 GPC (taxonomy) into one typed, per-100g-normalized
   response — so a consumer never reconciles three vendors themselves.
2. **Honest data, graded confidence.** Provenance, `cached`, and
   `category_hierarchy_source` travel with every response; a verified
   classification is distinguishable from a guess, and a heuristic is never
   dressed up as ground truth (see NOTES.md on GTIN→GPC being inherently
   heuristic).
3. **Fast and cheap by locality.** Local bulk mirrors and layered caches make
   the common request a microsecond disk read, keep the service inside the
   upstreams' rate limits (OFF's 15/min is the binding constraint), and let
   it run on a small self-hosted box.
4. **Degrade, never 500.** Partial answers over outages; the service's
   availability is not the intersection of its upstreams'.
5. **Publicly reachable, defensibly so.** Exposed to the internet (currently
   via Tailscale Funnel) with rate limiting, input caps, a firewalled
   backend, and attribution obligations honored.

## 1. Custom domain (`nutrition-api-dev.mcgarrah.org`) via Tailscale Funnel raw TCP forwarding

**Status:** shelved — revisit once the domain finishes migrating from
Squarespace to Porkbun. Not started.

**Goal:** serve the site at `https://nutrition-api-dev.mcgarrah.org` with a
real, publicly-trusted certificate (no browser warning), while keeping the
properties that made Tailscale Funnel attractive in the first place: no
router port-forwarding, no public IP exposure, no third-party proxy
(Cloudflare) in the request path.

### Why the obvious approaches don't work

- **Plain CNAME to the Funnel `*.ts.net` hostname.** Resolves fine, but TLS
  fails. Tailscale Funnel's default HTTPS mode terminates TLS itself at
  Tailscale's edge using a certificate scoped to your tailnet's own
  `*.ts.net` name — it has no mechanism to obtain or present a cert for an
  arbitrary custom domain. A browser requesting
  `nutrition-api-dev.mcgarrah.org` gets a cert for
  `nutrition-api-dev.squeaker-interval.ts.net` back, the names don't match,
  and the handshake fails (this exact failure is reported upstream:
  [tailscale/tailscale#16478](https://github.com/tailscale/tailscale/issues/16478)).
  Custom-domain support for Funnel is a still-open feature request as of
  2026-07: [tailscale/tailscale#11563](https://github.com/tailscale/tailscale/issues/11563).
- **Cloudflare in front (proxied CNAME).** Would work — Cloudflare terminates
  the custom domain's TLS itself and connects to the `*.ts.net` Funnel URL as
  its origin, sending correct SNI. Explicitly ruled out by the user: don't
  want to move DNS management to Cloudflare or add it to the request path.
- **Drop Funnel, port-forward 80/443 to this LXC, Caddy's own ACME via
  HTTP-01** (`deploy/Caddyfile`'s "Option B", already documented). Works, and
  is the fallback if the approach below doesn't pan out, but reintroduces
  the public-IP/port-forward exposure that Funnel was adopted specifically to
  avoid.

### The approach: Funnel raw TCP forward + Caddy's own DNS-01 cert

Tailscale Funnel has a **raw TCP forwarding mode** that does not terminate
TLS at all — it forwards the encrypted bytes untouched to a local port,
letting the local service (Caddy) perform its own TLS handshake with
whatever certificate it wants. Confirmed against Tailscale's current CLI
reference (`tailscale.com/docs/reference/tailscale-cli/funnel`, fetched
2026-07-17):

```
tailscale funnel --tcp=<port> tcp://localhost:<local-port> [off]
tailscale funnel --tls-terminated-tcp=<port> tcp://localhost:<local-port> [off]
```

**The two TCP flags are easy to confuse and do opposite things:**

- `--tcp=<port>` — **raw** forwarder. "By default, the TCP forwarder forwards
  raw packets." Tailscale does *not* touch TLS; the encrypted stream reaches
  the local backend untouched. **This is the one we want** — it lets Caddy
  see the real ClientHello and terminate TLS itself with a cert matching the
  custom domain.
  It's the same flag used with `--proxy-protocol=2` in Tailscale's own docs
  example (`tailscale funnel --proxy-protocol=2 --tls-terminated-tcp=443
  tcp://127.0.0.1:9899` — note that specific example pairs proxy-protocol
  with the *other* flag, since PROXY protocol there is about preserving the
  original client IP through Tailscale's own termination; don't copy that
  example verbatim, it terminates with the wrong cert for this use case).
- `--tls-terminated-tcp=<port>` — Tailscale terminates TLS using its own
  `*.ts.net`-scoped certificate, then forwards the **decrypted** stream to
  the local backend. This is for TCP protocols that want Funnel's automatic
  HTTPS but don't want to deal with certs themselves (their examples: SSH,
  RDP). **Wrong flag for a custom domain** — would just re-create the
  cert-mismatch problem one layer down.

Allowed Funnel ports either way: `443`, `8443`, `10000` only.

**Why DNS-01, not HTTP-01:** Funnel only forwards those three ports — port 80
is never reachable from the public internet on this box, so Caddy's default
HTTP-01 ACME challenge (which needs port 80) can't complete. Caddy needs a
**DNS-01** challenge instead: it proves domain ownership by creating a TXT
record via the DNS provider's API, which doesn't need any inbound port at
all. Since the domain is moving to Porkbun, this needs a Caddy build with the
Porkbun DNS provider plugin (`github.com/caddy-dns/porkbun`) — either via
`xcaddy build --with github.com/caddy-dns/porkbun` or by selecting it on
caddyserver.com's "Download" page — plus a Porkbun API key/secret with DNS
edit permission for the zone.

### Sketch of the resulting config

```
tailscale funnel --bg --tcp=443 tcp://localhost:8443
```

```caddyfile
nutrition-api-dev.mcgarrah.org {
	tls {
		dns porkbun {
			api_key {env.PORKBUN_API_KEY}
			api_secret_key {env.PORKBUN_API_SECRET}
		}
	}
	bind 127.0.0.1
	listen :8443
	import nutrition_api
}
```

(Exact directive names/shape to be verified against the `caddy-dns/porkbun`
plugin's own docs when this is picked up — not yet checked in this pass.)

### Bonus: single front door for multiple internal services

This pattern generalizes past just this one service, which is worth keeping
in mind given the Proxmox homelab has multiple LXCs/VMs that could each want
a public hostname. The clean version is **one** Caddy instance running
Funnel's raw `--tcp=443` forward, with one site block per custom
domain/subdomain (each getting its own DNS-01 cert), reverse-proxying
internally over the **tailnet's own private network** (MagicDNS names or
`100.x.x.x` addresses — not Funnel, not the public internet) to whichever
LXC/VM actually runs each backend. Only that one front-Caddy node needs
Funnel enabled at all; the backend nodes just need tailnet membership, no
Funnel of their own. Worth a dedicated design pass if/when there's a second
service wanting public exposure — not needed to unblock this one item.

```mermaid
flowchart LR
    internet["Public internet"]

    subgraph front["Front-Caddy LXC — the only node running Funnel"]
        f["tailscale funnel --tcp=443<br/>(raw, untouched by Tailscale)"]
        c1["nutrition-api-dev.mcgarrah.org<br/>DNS-01 cert"]
        c2["other-service.mcgarrah.org<br/>DNS-01 cert"]
    end

    subgraph tailnet["Tailscale private network (not Funnel)"]
        direction TB
        n1["nutrition-api LXC<br/>100.x.x.x, no Funnel"]
        n2["other-service LXC/VM<br/>100.x.x.x, no Funnel"]
    end

    internet -->|"raw TLS bytes"| f
    f --> c1
    f --> c2
    c1 -->|"reverse_proxy over tailnet"| n1
    c2 -->|"reverse_proxy over tailnet"| n2
```

### Open questions before starting

- Exact `caddy-dns/porkbun` Caddyfile directive syntax and whether it needs a
  custom-built Caddy binary replacing the apt-installed one on the reference
  LXC (`deploy/README.md`'s "Install" section currently documents the plain
  apt package, which won't have any DNS plugin compiled in).
  This is the biggest unknown — apt's Caddy build has no DNS providers,
  so switching means either `xcaddy` building locally or an unofficial repo;
  worth evaluating both before committing.
- Whether Porkbun's API is fully live for this zone yet, or still mid-migration
  from Squarespace.
- Confirm no separate Tailscale plan/tier gate exists on `--tcp` raw
  forwarding (nothing found in the docs fetched 2026-07-17, but wasn't
  exhaustively checked against Tailscale's pricing/plan pages).
- Decide whether to keep the existing `:8090` plain-HTTP loopback Funnel
  block (current working `*.ts.net` setup) running alongside this, or replace
  it outright once the custom domain is verified working.

## 2. Name search: stop blocking the event loop, then make it fast (FTS5)

**Status:** (a) fixed 2026-07-18. (b) not started.

**(a) ~~The search endpoint blocks the event loop~~ — FIXED.** `search_by_name`
in `app/core/search_routes.py` was `async def`, but `search.search_products()`
runs synchronous `sqlite3` queries directly — no thread pool, no
`run_in_executor`. Measured on the reference LXC: a cold-cache
`LIKE '%peanut butter%'` scan over `off.sqlite3` (1.2 GB) took **~17 s**
(≈0.65 s warm). For that whole time the worker's event loop was stalled —
every other request on that worker, including barcode lookups and health
checks, waited. Fixed by making the route `def` instead of `async def` —
FastAPI/Starlette runs a sync path operation in its threadpool automatically,
which is what CLAUDE.md's own rule (async handlers are for *external* I/O)
argues for here anyway, since the work is local blocking disk I/O, not a
network call. Guarded by a regression test
(`test_search_route_is_sync_so_fastapi_runs_it_in_the_threadpool`) that fails
loudly if the route ever goes back to `async def`.

**(b) ~~Leading-wildcard `LIKE` can never use an index~~ — FIXED 2026-07-18.**
Both mirrors were scanned with `LIKE '%q%'`, a full-table scan by
construction. `scripts/build_fdc_db.py` / `build_off_db.py` now build an
FTS5 virtual table (`foods_fts` / `products_fts`, schema_version bumped to
`"2"`) over (`gtin14` UNINDEXED, `description`/`product_name`,
`unicode61 remove_diacritics 2` tokenizer) alongside the real table.
`app/core/search.py` queries FTS5 first — each query word becomes a quoted,
prefix-matched token (`"peanut"*`), ANDed together, ordered by FTS5's `rank`
— and falls back to the old `LIKE` scan when a mirror predates the schema
change (checked via `sqlite_master`, not a version number, so an unrefreshed
mirror degrades gracefully rather than erroring).

Measured against a live copy of the real 1.2 GB OFF mirror, not just the
unit-test fixtures: building the index took **8.7s** and added **~100 MB**
(1.2 GB → 1.3 GB — well under the "few hundred MB" estimate). Query latency:
**9–98 ms** warm (`"organic milk"` 9ms, `"peanut butter"` 24ms, the single
common word `"chocolate"` 98ms — worst case is a single very-frequent word,
since `ORDER BY rank` has to score every match before the `LIMIT`), against
the prior ~17s cold / 0.65s warm `LIKE` scan. Test coverage: new unit tests
for `_fts_match_expr` and the FTS query path in `test_search.py`, and for
the two build scripts' new virtual table in `test_fdc_bulk_import.py` /
`test_build_off_db.py`. Full suite: 804 passed.

Not yet done: the **already-built, currently-deployed** `data/fdc.sqlite3`
and `data/off.sqlite3` predate this change and have no FTS table — they will
correctly fall back to the `LIKE` path (slow but not broken) until the next
`build_fdc_db.py` / `build_off_db.py --auto-update` rebuild republishes them.
Tokenizer behavior on OFF's non-English/accented product names was not
separately audited — `remove_diacritics 2` should handle the common case,
but this wasn't stress-tested against real multilingual rows.

## 3. Scheduled refresh of the local bulk mirrors

**Status:** not started. Currently a fully manual loop.

OFF republishes daily and FDC twice a year, but refreshing the mirrors
means remembering to run `build_off_db.py` / `build_fdc_db.py
--auto-update`, then `gh release create`/`upload` the `.xz` archives, then
restart the service. Nothing schedules it, so the OFF mirror silently ages
(the one on the reference LXC is `off-2026-07-14` and only that fresh
because it was rebuilt by hand during the nutrient-expansion work).

Sketch: a systemd **timer** on the reference LXC (weekly for OFF — daily is
churn without benefit given the response store absorbs misses; on-release
for FDC, which `--check` already detects) running a script that rebuilds,
verifies row counts against the previous build (a >10% shrink aborts —
upstream export glitches happen), publishes the release asset with
`gh release upload --clobber`, and restarts `nutrition-api.service` off-peak.
Decisions to make when picking this up: whether the LXC (12 GB RAM) is the
right build host or whether a beefier build node should push assets; where
build logs/failures surface (the `/status` dashboard already shows dataset
dates, so staleness is at least *visible* today); and whether to prune old
dated OFF downloads (they are deliberately kept side-by-side today).

## 4. ~~Upgrade the OFF fuzzy GPC matcher to the word-boundary prototype~~ — DONE 2026-07-18

Promoted into `app/core/gpc_match.py` as `fuzzy_hierarchy_for_off_categories`,
replacing `orchestrator._fetch_gpc_categories`'s inline substring-`LIKE`
query. Full detail and real-corpus measurements are in ARCH.md, "GPC
Category Matching" — summary here:

- Built on an FTS5 index over brick descriptions (`bricks_fts`, added to
  `scripts/import_gpc_xml.py`'s schema, same technique as item 2's product
  search) rather than a hand-rolled word index — `ORDER BY rank` (bm25)
  does the "prefer the least common word" job the original prototype did by
  hand, and word-boundary matching comes from FTS5 directly rather than
  needing to be built.
- Tags are tried narrowest-first (`reversed`, capped at `_MAX_TAGS_TRIED =
  8`), fixing the wrong-order bug (old code took the first three — the
  broadest — tags).
- A stopword list (`beverages`, `food`, `products`, `drinks`, plus
  grammatical connectors) built from real frequency counts across ~200k
  OFF products' category tags, not guessed.
- Falls back to a (still stopword-filtered, still narrowest-first) `LIKE`
  scan for a `gpc.sqlite3` built before `bricks_fts` existed, checked via
  `sqlite_master` — the same graceful-degradation pattern as item 2.

**Measured against the real corpus** (random 2,000-product sample of
`data/off.sqlite3`): old matcher 53.1% match rate, of which **18.6% were
literally the documented `Alcoholic Beverages Variety Packs` bug**; new
matcher 93.0% match rate, and a direct check of five real
`en:beverages`-tagged pasta products confirms none produce that wrong
answer any more. **Not measured**: a new overall precision/noise figure to
replace the original 69% — that required the original investigation's
manual case-by-case audit, not repeated here, so treat the higher recall as
real but the precision improvement as plausible-and-partially-verified
rather than fully quantified. The polysemy ceiling (`beans`, `spring`) is
unchanged, as expected — it was never a matching-mechanism problem.

Test coverage: unit tests for `_meaningful_words`/`_fts_match_expr`, an FTS
fixture (`gpc_db_with_fts`) exercising the real query path including a
regression test that `"ola"` (substring of "Cola" but not a token prefix)
does *not* match — the exact class of bug FTS5's prefix matching (vs. the
old substring `LIKE`) rules out — plus the LIKE-fallback path via the
existing `gpc_db` fixture. Full suite: 821 passed.

**Left for later, not blocking:** a real manual precision audit at the
scale of the original investigation (item 5, the `reviewed` tier, is
designed to absorb exactly this kind of review effort once it exists,
rather than repeating a one-off audit here).

## 5. `reviewed` tier for fuzzy GPC matches

**Status:** designed-for but not started. Blocked conceptually on item 4 —
review effort is better spent on the improved matcher's output than the
current 69%-noise one.

`category_hierarchy_source` was deliberately typed so adding a `reviewed`
value is additive, not breaking (see `models.py` and ARCH.md). The missing
piece is the review workflow itself: persistence for (OFF tag →
brick, verdict) pairs — a small SQLite table beside the GPC cache fits the
existing pattern; a review UI, most naturally an extension of the existing
`/gpc/mappings` viewer, which already shows coverage and uncovered
categories; and orchestrator precedence slotting reviewed matches between
`fdc_curated` and `off_fuzzy`. Scope control: review the *head* of the
distribution (most-frequent OFF tags first) — the same Pareto logic that
made FDC curation tractable (top 90 categories → 90.7% coverage there).
