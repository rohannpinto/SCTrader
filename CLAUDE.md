# CLAUDE.md — Star Citizen Trading Route Optimizer

This file is the **single source of shared context** for every implementer
and reviewer agent working on this project, task by task. Each agent reads
only this file plus its own task-specific instructions — not the original
design plan. If something you need isn't here, that's a gap worth flagging,
not a reason to guess.

## Project summary

A local, all-Python full-stack web app that computes efficient Star Citizen
commodity trading routes: given a starting station/terminal, a selected
ship (its quantum drive range and cargo capacity), a hop-count budget, and
a starting cash balance, it finds the walk through the in-game trade
network that maximizes final cash on hand, using live commodity price data
pulled from public APIs. Backend is **FastAPI** (Python 3.11+) with **SQLite** (via
SQLAlchemy) as a local on-disk cache of API data, and an in-memory
**networkx `DiGraph`** built from that cache. Frontend is **Streamlit**,
talking to the backend over local HTTP only. No auth, no deployment concerns
right now — this all runs on one machine. There is a real, tentative future
plan to deploy this to a public AWS EC2 instance; application-level security
hardening is being built now in anticipation of that (see "Security ground
rules" below), but infrastructure-level protections (WAF, ALB, Shield, TLS,
Secrets Manager, CloudWatch) are explicitly out of scope until that move
actually happens.

## Data model & search resources (Phase 2: hop count, cash, cargo)

**Phase 1 note:** this section originally described a continuous
distance-budget/edge-weight-formula model with Pareto-frontier dominance
pruning. Phase 2 (Task 14) replaced that model outright — a ship's quantum
range now gates which edges are even traversable (a hard per-hop filter,
not a weight denominator), the search is bounded by an integer hop count
instead of a distance budget, and the traveler's real cash/cargo are
tracked so profit is an absolute, quantity-scaled aUEC amount, not a
per-unit rate. This rewrite describes the current model precisely; nothing
below should be read as "the same as before, just reworded."

- **Node** = a single trade-capable terminal (not a whole station — a
  station can host multiple terminals with different prices). Non-commodity
  terminals (ship dealers, refuel-only, etc.) are filtered out during
  ingestion. Unchanged from Phase 1.
- **Edge a→b** exists for every known directed distance between two
  commodity-trading terminals (from the `Distance` table) — the graph
  itself is **not** pre-filtered by any distance threshold at build time.
  Each edge carries exactly one attribute: `distance` (the raw, unfloored
  gigameters value from the `Distance` row). Per-hop traversal is filtered
  at *search* time by the **selected ship's quantum range**
  (`ship_jump_range_gm`, sourced from the `Ship` the user picked) — an edge
  is only traversable if `edge.distance <= ship_jump_range_gm`. This fully
  replaces Phase 1's `distance_threshold` request parameter; there is no
  standalone `DISTANCE_THRESHOLD`/`distance_threshold` concept anymore.
- **No precomputed edge weight/profit/best_commodity.** Which commodity is
  profit-maximizing on an edge now depends on how much cash the traveler
  has *when they arrive* at the edge's origin — a search-time fact, not a
  graph-build-time one — so `backend/graph/builder.py` no longer computes
  any of that. It still bulk-loads `buy_prices`/`sell_prices`
  (`terminal_id -> {commodity_id: price}`, one query each, same
  missing-vs-zero discipline as always — a `None`/absent price is *never*
  inserted, so "missing" and "known to be zero" stay distinguishable) and
  returns them via `GraphBuildResult`; `backend/graph/cache.py`'s
  `GraphCacheSnapshot` carries `graph`, `buy_prices`, and `sell_prices`
  together, built once per refresh and atomically swapped in as one unit —
  same "build once per refresh, not once per request" performance property
  as the graph itself.
- **Per-hop commodity/quantity selection (cash- and cargo-aware), done at
  search time in `backend/graph/search.py`:** for edge `a→b`, given `cash`
  on hand at `a` and the selected ship's `ship_cargo_capacity_scu`: for
  each commodity `c` with **both** a valid buy price at `a` **and** a valid
  sell price at `b` (set-intersection of the two indices — a missing price
  excludes the commodity from consideration entirely, never treated as
  `0`, exactly as before):
  - `quantity_c = min(floor(cash / buy_price(c, a)), ship_cargo_capacity_scu)`
    when `buy_price(c, a) > 0`; else `quantity_c = ship_cargo_capacity_scu`
    (a free/non-positive-cost commodity — extremely unlikely given Task
    13's curated commodity set, but handled correctly rather than dividing
    by zero).
  - `profit_c = quantity_c * (sell_price(c, b) - buy_price(c, a))`, only a
    candidate when `sell_price(c, b) > buy_price(c, a)`.
  - The commodity maximizing **total** `profit_c` wins — not per-unit
    margin. This is the whole point of cash-awareness: a cheaper commodity
    with a more affordable quantity can beat a pricier one with a better
    per-unit margin once cash is limited.
  - If no commodity is profitable (or cash is `0` and nothing is free),
    the hop is a neutral **"bridge" hop**: `quantity = 0`, `profit = 0`,
    `commodity_id = None`, cash unchanged — same bridge-hop concept as
    Phase 1, just cash-aware now. The edge still exists and still costs a
    hop — occasionally useful to reach a profitable edge further along.
- **Simplifying assumption, stated explicitly rather than silently
  assumed:** 1 tradeable unit ≈ 1 SCU of cargo space for the cargo-cap
  calculation above. The real game has per-commodity box sizes; this is
  fine for realistic routing, not a perfect SCU accounting model. Revisit
  only if this turns out to matter in practice.
- **`min_distance_floor`'s role today:** applied **once, at ingestion
  time** (`backend/ingest/refresh.py`), to give same-orbit terminal pairs a
  sensible nonzero *stored* `Distance.distance`. It is **not** a
  divide-by-zero guard in a weight formula anymore — no such formula
  exists in Phase 2. Neither `backend/graph/builder.py` nor
  `backend/graph/search.py` reads `min_distance_floor` at all.

## Route search problem

**The resource being spent is now an integer hop count, not a continuous
distance budget.** Starting from a fixed node with `starting_budget` cash
and a ship of `ship_cargo_capacity_scu` cargo capacity, find the walk
(revisits/cycles allowed) of **at most `num_hops` hops** that ends with the
most cash on hand. Each hop must cross an edge whose raw
`distance <= ship_jump_range_gm` (see above). `max_distance` no longer
exists as a concept anywhere in this app.

- **State is `(node, hops_used, cash)`.** Implemented in
  `backend/graph/search.py` as a **plain bounded dynamic program**, not a
  priority-queue label-setting search: for `hop` from `1` to `num_hops`,
  for every node with a label at `hop - 1`, try every valid (jump-range-filtered)
  outgoing edge, compute the resulting cash via the per-hop commodity/quantity
  selection above, and keep only the single best (max-cash) label per
  `(node, hop)` — a `dict`-backed table, overwriting only on strict
  improvement.
- **No Pareto-frontier dominance pruning — a proven simplification, not a
  shortcut.** At a fixed `(node, hops_used)`, more cash always **weakly
  dominates** less cash: a higher-cash label can always replicate every
  future buying decision a lower-cash label at the same `(node, hops_used)`
  would make, because `quantity` is monotonically non-decreasing in `cash`
  and capped by the *same fixed* `ship_cargo_capacity_scu` regardless of
  which label is being extended — so the higher-cash label ends up with at
  least as much cash at every subsequent hop too. There is no second axis
  to trade off the way `distance_used` traded off against
  `cumulative_weight` in Phase 1. This is why a single best-cash label per
  `(node, hop)` is provably sufficient — no label cap, no per-node cap
  setting, no priority queue needed at all. The entire state space is
  `O(nodes * num_hops)`, already small and exactly bounded by construction,
  not something that needs a heuristic top-K safety valve to stay
  tractable.
- **Parent-pointer path reconstruction, from the start.** A label is
  `(node, hops_used, cash, previous: Label | None, ...per-hop trade
  details)`. `previous` is a parent pointer, never a materialized path —
  this project already hit and fixed a real O(depth) performance bug from
  exactly the opposite mistake in the Phase 1 algorithm (`backend/graph/
  search.py`'s git history: "search.py O(depth) path storage" —
  concatenating a new tuple onto a path at every hop made label creation
  itself O(depth) and left many long-lived labels each holding an
  independent long tuple, dominating wall time in post-search
  reference-counting teardown). The Phase 2 algorithm is built with that
  lesson already applied: every label is O(1) to create, and only the
  single winning label, once, ever pays to walk its `previous` chain back
  to the start.
- **Termination is now structural, not merely resource-bounded.** The DP is
  a fixed loop over `hop in 1..num_hops`; it is never a frontier that could
  keep re-queuing a profitable cycle. Unlike Phase 1 (where a naive
  flipped-comparator Dijkstra would have looped forever exploiting a
  positive-weight cycle, which is exactly why Phase 1 needed budget +
  dominance pruning to terminate), Phase 2's algorithm structurally cannot
  hang regardless of how many profitable cycles the graph contains — it
  simply revisits the same node at a later `hop` with a higher `cash`.
- **Best answer = highest-cash label across every `(node, hop)` with `hop`
  from `0` to `num_hops` inclusive** — not only `hop == num_hops`. A
  dead-end node has no way to "pad" a great short route out to the full hop
  count with neutral bridge hops, so requiring the exact count would
  wrongly disqualify it.
- **Anytime behavior / safety valve:** `settings.search_time_budget_seconds`
  bounds total wall-clock time, checked once between fully-computed hop
  levels (never mid-level, so a firing deadline always leaves the DP table
  in a complete, consistent state to compute the best answer from) — cheap
  defense in depth, not expected to realistically fire given the tightly
  bounded state space this algorithm actually explores. There is no
  per-node label cap setting in Phase 2 (Phase 1's
  `search_label_cap_per_node` no longer applies to anything).
- **"Found" vs. "no profitable route":** `found=True` iff the best cash
  found is **strictly greater** than `starting_budget` — real, positive
  realized profit, not merely "didn't lose money." This differs from
  Phase 1's `cumulative_weight > 0` check (weight could never go negative
  there; cash very much can stay exactly flat across a walk made entirely
  of bridge hops, and that must not be reported as "found").
- **Isolated start node:** if the chosen start terminal has zero viable
  outgoing edges under the selected ship's quantum range, this falls out
  naturally with no special-casing needed — no hop-1 labels get created at
  all, the only candidate is the `hop=0` starting label itself
  (`cash == starting_budget`), which is not strictly greater than
  `starting_budget`, so `found=False`.

## Two-tier caching architecture

1. **Disk tier (source of truth):** SQLite cache DB (`data/cache.db`, WAL
   mode) populated by the ingestion pipeline (`backend/ingest/refresh.py`)
   from the two API clients. This is what `/refresh` rewrites.
2. **In-memory tier:** a `GraphCache` singleton (`backend/graph/cache.py`)
   holds the current `networkx.DiGraph`, built **once per refresh, not once
   per request**. It exposes `get_graph()` (instant, returns the held graph)
   and `rebuild()` (called once, right after a refresh completes, and once
   at backend startup to pre-warm from whatever's already on disk).
   Routers depend on `get_graph()` only — never on the builder directly.
   - **Atomic swap, not in-place mutation:** `rebuild()` constructs the
     *new* graph as a fresh object, then swaps the module-level reference in
     one step, so an in-flight `/route` request always sees either the
     fully-old or fully-new graph, never a half-populated one — no locking
     needed on the read path.
   - **Refresh overlap guard:** an in-process lock ensures a second refresh
     request while one is in flight gets `409`, never runs concurrently
     with the first (protects against double-hitting external APIs and
     racing cache writes).
   - A small **LRU cache on `/route` results**, keyed on the request's
     search parameters plus `cache_data_version` (Phase 1:
     `(start_terminal_id, max_distance, distance_threshold,
     cache_data_version)`; Phase 2 replaces the middle two with the new
     request shape — `(start_terminal_id, ship_id, num_hops,
     starting_budget, cache_data_version)`, per Task 15's schema/router
     rework), so repeated/lightly-tweaked queries against the same
     refreshed dataset return instantly. The data-version component makes
     it self-invalidating on every refresh.

## Project structure

```
StarCitizen Trader/
  backend/
    main.py                 # FastAPI app, routers, CORS, rate limiter, error handler
    config.py                # pydantic-settings: API base URLs, thresholds, DB path,
                              #   REFRESH_TOKEN, rate-limit values, shared httpx timeout/retry policy
    logging_config.py        # structured logging setup
    clients/
      wiki_client.py         # api.star-citizen.wiki client (commodities, prices)
      uex_client.py           # UEX terminals_distances client
    models/
      db.py                   # SQLAlchemy models: Commodity, Terminal, Price, Distance
      schemas.py               # Pydantic request/response models
    ingest/
      refresh.py               # orchestrates clients -> cache DB, ID joining, filtering
    graph/
      builder.py                # cache DB -> networkx DiGraph + bulk buy/sell price indices (pure)
      search.py                  # bounded hop-count/cash/cargo DP search (Phase 2)
      cache.py                    # GraphCache singleton: build-once-per-refresh, atomic swap
    routers/
      terminals.py                # GET /terminals
      refresh.py                   # POST /refresh, GET /refresh-status
      route.py                      # POST /route (LRU-cached by query + data version)
  frontend/
    app.py                    # Streamlit UI, calls backend over localhost
  tests/
    fixtures/                 # recorded JSON responses from both APIs
    unit/                     # price indices, graph builder, search algorithm
    integration/               # refresh pipeline + FastAPI TestClient
    perf/                      # synthetic large-graph benchmark (wall-clock budget)
  scripts/
    refresh_data.py            # CLI entrypoint for a manual cache refresh
  data/                       # sqlite cache db (gitignored)
  CLAUDE.md                   # shared conventions for every sub-agent
  README.md
  requirements.txt
  .env.example
  .gitignore
```

As of Task 0, only the directories/`__init__.py` files and the files listed
under "How to run" below actually exist with real content. Everything else
in the tree above is a placeholder for a specific later task — do not
create empty stub files for modules you're not currently implementing;
create them when the task that owns them starts.

## How to run

From the project root (`StarCitizen Trader/`):

```powershell
# Create/activate the venv (already created in Task 0 as .venv)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install pinned dependencies
pip install -r requirements.txt

# Run the backend (from the project root, so `backend` is importable)
uvicorn backend.main:app --reload

# Run the frontend (separate terminal, venv activated)
streamlit run frontend/app.py

# Tests
pytest                 # default suite: fast, offline, respx-mocked (live + perf excluded)
pytest -m live         # opt-in: hits the real external APIs once, checks for schema drift
pytest -m perf         # opt-in: synthetic large-graph performance benchmark

# Manual data refresh (once ingest/refresh.py + scripts/refresh_data.py exist)
python scripts/refresh_data.py
# or, with the backend running:
# POST http://localhost:8000/refresh  (with X-Refresh-Token header if REFRESH_TOKEN is set)
```

Copy `.env.example` to `.env` and adjust values as needed; `.env` is
gitignored and read automatically by `backend/config.py` via
`pydantic-settings`.

## Data source implementation notes

Empirical findings from Task 2 (`wiki_client.py`) and Task 3
(`uex_client.py`)'s research spikes, recorded here so later tasks (Task 4's
ingestion pipeline especially) don't have to re-derive them. Full detail
and evidence lives in each client's module docstring — this is the
shared-context summary.

- **Wiki API base URL confirmed:** `https://api.star-citizen.wiki/api` (the
  original placeholder in `backend/config.py` needed no change).
- **UEX API base URL corrected:** use `https://api.uexcorp.uk/2.0`, **not**
  `https://uexcorp.space/api/2.0` (the original placeholder). Both front
  the same API/dataset, but `uexcorp.space` sits behind Cloudflare
  bot-management that fingerprints the TLS/HTTP client itself — it blocks
  Python `httpx` even with a full browser-like header set (not a
  User-Agent-string check, so adding headers doesn't fix it). `api
  .uexcorp.uk` has no such block and needs zero special headers. No API key
  is required for any endpoint this app uses (`terminals`,
  `terminals_distances`, `orbits_distances`, `star_systems`).
- **Wiki-to-UEX terminal ID join is direct and clean — no fallback
  needed.** The wiki API's `terminal_id`/`terminal_code` fields (on each
  commodity's `uex_prices.purchase[]` entry) are UEX's own terminal `id`/
  `code` values, verbatim (the wiki API sources this data from UEX in the
  first place). Verified exactly across every entry in a real fixture
  (26/26 matched). Task 4 should join on `terminal_id == id` directly; the
  design plan's name+system fallback contingency should not be needed.
- **Bulk distance ingestion: use `orbits_distances`, not per-pair
  `terminals_distances`.** `terminals_distances` only supports a
  single-pair lookup (`id_terminal_origin` + `id_terminal_destination`,
  both required) — useful for spot checks/fallback, but a full distance
  matrix via that endpoint alone would be up to ~12,880 individual
  requests for the 161 real commodity terminals. `orbits_distances
  ?id_star_system={id}` instead returns **every** orbit-pair distance
  within a system in one call (an "orbit" is coarser than a terminal —
  each terminal has one parent `id_orbit`, and the 161 commodity terminals
  collapse onto only 49 distinct orbits). Only 3 star systems are
  currently available/playable (`is_available == 1` on `/star_systems`:
  Nyx=55, Pyro=64, Stanton=68 — derive this dynamically, don't hardcode
  it, so a newly-live system is picked up automatically). One
  `/star_systems` call plus one `/orbits_distances` call per available
  system (4 requests total today) gives complete, verified-zero-gap
  coverage of every orbit pair needed to connect all real commodity
  terminals (Nyx 42/42 directed pairs, Pyro 272/272, Stanton 600/600).
  Distances are **directional, not reliably symmetric** (a small number of
  Stanton pairs, all touching a jump-point-gateway orbit, differ by
  direction) — store/look up both directions rather than assuming
  symmetry. `UexClient.iter_all_orbit_distances()` /
  `.list_all_orbit_distances()` implement this bulk strategy end to end.
- **Vehicles bulk listing (Task 11, `wiki_client.py`'s `iter_vehicles`/
  `list_all_vehicles`):** `GET /vehicles` uses the exact same Laravel-style
  paginated envelope and page-size clamp (observed max `200`) as
  `/commodities` — but, unlike `/commodities`, each list item is already
  the **full** vehicle record (`quantum`, `cargo_capacity`, `manufacturer`,
  and everything else a per-item detail call would also return), so there
  is no separate detail-fetch step for ingestion to do. 295 total vehicles
  across 2 pages at the clamped size, as of the capture date (2026-08-15).
  Filter for the `Ship` table, verified against all 295: keep
  `is_spaceship == true` **and** `quantum.quantum_range` non-null. Of 295
  vehicles, 247 have `is_spaceship: true`; the other 48 (ground vehicles,
  gravlev craft, power-suit "vehicles") all have `quantum.quantum_range:
  null`, with no exceptions, confirming `is_spaceship` alone is a reliable
  gate. Of the 247 real spaceships, 10 have `quantum.quantum_range: null`
  (small craft with no quantum drive at all — `MPUV Cargo`/`Personnel`/
  `Tractor`, `Pitbull`, `P-52 Merlin`, both `P-72 Archimedes` variants, all
  three `Fury` variants) and must be skipped (with a logged warning), not
  defaulted to `0.0`, which would be indistinguishable from a real
  zero-range reading — so 237/247 (96%) of real spaceships carry usable
  data. `cargo_capacity` was never observed `null` for a real spaceship (0
  of 247) but is frequently a genuine, present `0` (117 of 247 — mostly
  pure fighters/interceptors with no cargo grid), so a present `0` is kept
  as-is, never treated as missing — but a genuinely-`null` `cargo_capacity`
  is skipped (logged warning) exactly like a `null` `quantum_range`, not
  coerced to `0.0`, which would be indistinguishable from a real
  zero-cargo fighter. `manufacturer`/`manufacturer.name` was never observed
  `null` for any of the 237 usable spaceships. **Unit confirmed meters**:
  cross-checked the Caterpillar's `quantum_range: 70284406669` (→ `70.284`
  after `/ 1e9`) against its publicly known ~70 million km quantum range
  spec — matches once treated as meters → gigameters, the same unit the
  `Distance` table already uses. Sanity range across all 237 usable
  spaceships: `41.74` Gm (small fighters) to `10204.08` Gm (the `F8A
  Lightning`, an outlier by design; capital ships like the `Idris-P` sit
  around `1957` Gm) — all strictly positive, all the same unit, consistent
  with meters at Star Citizen's in-lore scale.
- **Task 12 — orbital-station `planet_name`/`moon_name` association,
  confirmed empirically against live data (2026-08-15):** the open question
  was whether an orbital station's own `planet_name`/`moon_name` field on
  `GET /terminals` actually identifies what it orbits, or is simply never
  populated for stations. **It does, for most stations, but not all.**
  Queried `GET /terminals?type=commodity` live (161 real commodity
  terminals): 67 are orbital stations (`space_station_name` set). Of those
  67:
  - **48 (72%) have `planet_name` populated**, correctly identifying the
    orbited planet — e.g. all five ArcCorp Lagrange stations (`ARC-L1`…
    `ARC-L5`) carry `planet_name: "ArcCorp"`; Baijini Point (orbits
    ArcCorp) and Everus Harbor (orbits Hurston) both confirmed exactly as
    the design plan's example expected.
  - **2 (both GrimHEX's terminals) have `moon_name` populated** (`"Yela"`)
    — and in that case `planet_name` is *also* populated (`"Crusader"`,
    `Yela`'s parent planet), not left null. So a station orbiting a moon
    gets both fields filled in, not just the moon.
  - **19 (28%) have neither field set** — confirmed to be *exclusively*
    two categories: (1) deep-space interstellar jump-point gateways (e.g.
    `TERGAT`/"Terra Gateway (Stanton)", `PYROG`/"Pyro Gateway (Stanton)",
    and the matching gateway terminals in Pyro and Nyx pointing back at
    the other two systems), which don't orbit any specific planetoid by
    game design; and (2) Nyx's four "People's Service Station"
    terminals (`PSSA`/`PSST`/`PSSL`/`PSSD`), which likewise have no parent
    planetoid on record in UEX's data (Nyx has no "planet" body in UEX's
    data model at all — only Delamar and various station/gateway orbits).
    These 19 terminals are the ones Task 16's frontend must expect to
    surface only under "Planetoid: All", never a specific planetoid filter
    — `is_orbital_station=True` with `planet_name`/`moon_name` both `None`.
  - For reference, ground/non-orbital terminals are nearly always fully
    populated: 92/94 have `planet_name`, 48/94 additionally have
    `moon_name` (an outpost on a moon carries both the moon and its parent
    planet — e.g. `ArcCorp Mining Area 045`: `planet_name: "ArcCorp"`,
    `moon_name: "Wala"`). The 2 exceptions are terminal 422 ("Admin - UEX
    Station", the pre-existing `id_orbit: 0` edge case from Task 4) and
    Levski (a Nyx city terminal on Delamar — again, Nyx has no "planet"
    body in UEX's model, consistent with the fact that every terminal in
    `tests/fixtures/uex_terminals_sample.json` — a small all-Nyx excerpt —
    has `planet_name: null` regardless of ground vs. orbital status).
  - Locked in for drift detection: `test_uex_client.py::
    test_live_orbital_station_planet_association_smoke` (checks Everus
    Harbor, Baijini Point, and Terra Gateway by name against live data,
    plus the "moon_name set implies planet_name set" invariant).
- **Task 13 — commodity curation allowlist, verified against the live
  catalog (2026-08-15):** `backend/ingest/refresh.py`'s
  `TRADEABLE_COMMODITY_GROUPS` allowlist (`Metal, Mineral, Nonmetal,
  Halogen, Alloy, Gas, UnrefinedOres, Raw_Minerals, SyntheticMaterials,
  Waste, Food, Organic, Vice`, OR semantics against a commodity's
  `commodity_groups`) was checked against every commodity the live wiki API
  actually returns, not just the fixtures. Pulled the full `GET
  /commodities` catalog (206 commodities) plus every commodity's detail
  response (for `commodity_groups`):
  - **157 pass, 49 excluded.** Group-by-group breakdown of the 49 excluded:
    43 `ProcessedGoods`-only, 11 `Bulk_Supplies` (all also tagged
    `ProcessedGoods` — ship ammunition sizes 1–9 plus decoy/noise
    countermeasures), 2 `HeatPlaceholder` ("Heat", "Cooler"), 2 `CleanAir`
    ("Molina Mold" and one item with an empty `name` field, slug
    `vipcryopod` — see the "surprising" note below), 1 `PowerPlaceholder`
    ("Power:"), 1 `LifeSupportPlaceholder` ("Life Support"). (Some
    commodities carry more than one group, so per-group pass/fail counts
    don't sum to 206/157/49 — e.g. every `Bulk_Supplies` item is also
    `ProcessedGoods`.)
  - **`Luminalia Gift` reconfirmed excluded live**, unchanged from the
    design plan's research: `commodity_groups: ["ProcessedGoods"]`, still
    a real, currently-returned catalog entry (not rotated out) — captured
    verbatim into `tests/fixtures/wiki_commodity_luminalia_gift.json` for
    the end-to-end exclusion test.
  - **Spot-checked every excluded group for a wrongly-excluded real
    material — found none.** All 43 `ProcessedGoods`-only items are
    genuinely non-repeatable-trade-good (seasonal items like `Luminalia
    Gift`/`Year of the Rat Envelope`; ship-loot cosmetics like `Ace
    Interceptor Helmet`/`RS1 Odysey Spacesuits`; quest/mission props like
    `Evidence Box`/`Organs`; and a handful — `Hydrogen Fuel`, `Quantum
    Fuel`, `EVA Fuel` — that are ship consumables, not sellable cargo).
    `Bulk_Supplies` is exactly ship ammo/countermeasures, as the design
    plan predicted. The four placeholder groups (`Heat`/`Cooler`/`Power:`/
    `Life Support`) are single-item engine internals, not real goods.
  - **28 `ProcessedGoods`-tagged commodities correctly still pass**, because
    they're *dual*-tagged with a second, legitimate group — e.g. `SLAM`,
    `Stims`, `Neon`, `Distilled Spirits` (`ProcessedGoods` + `Vice`) and
    `Bioplastic`, `DynaFlex`, `Diamond Laminate` (`ProcessedGoods` +
    `SyntheticMaterials`) — confirming the OR-semantics design decision
    (not "all groups must match") was the right call: a strict AND rule
    would have wrongly excluded every real refined drug and synthetic
    material in the game.
  - **Spot-checked every included group** (`Food`, `Organic`, `Vice`,
    `Metal`, `Mineral`, `Gas`, `Nonmetal`, `Halogen`, `Alloy`, `Waste`,
    `UnrefinedOres`, `Raw_Minerals`, `SyntheticMaterials`) — all real,
    recognizable materials/consumables (e.g. `Agricium`, `Laranite`,
    `Hydrogen`, `Fresh Food`, `Altruciatoxin`, `Scrap`), nothing surprising.
  - **Surprising, unrelated-to-the-allowlist finding:** two live commodities
    have an entirely empty `name` field (`""`) — `evidencebox` (excluded,
    `ProcessedGoods`) and `vipcryopod` (excluded, `CleanAir`); a third,
    `vlk-limpet` (included, `Organic`), also has an empty `name`. Pre-
    existing live-API data quality, not something this task's allowlist
    causes or needs to fix — noted here in case a later task (e.g. a
    frontend commodity picker) needs to handle a blank display name
    gracefully.
  - Locked in for drift detection: `test_refresh.py::
    test_live_commodity_catalog_allowlist_pass_exclude_counts_in_ballpark`
    (order-of-magnitude bounds, not exact counts — the live catalog can
    drift — plus a hard assertion that `luminalia-gift` specifically stays
    excluded and `laranite` specifically stays included).
- **Task 18 — zero price means "not traded in this direction," not "priced
  at zero," confirmed empirically at full scale (2026-08-15):** Task 2's
  original research established `price_buy`/`price_sell` are "always
  numeric, never JSON `null`" — true, but incomplete. A live end-to-end
  `/route` run (Task 17) produced a **$39.7 billion fake-profit route**,
  traced to `backend/graph/search.py` treating a real, ingested `price_buy
  == 0.0` as "free to acquire, buy the max cargo allows" — a defensive
  branch whose own comment called it "extremely unlikely given Task 13's
  curated commodity set." It is not unlikely; it is the dominant shape of
  real data. Recomputed the full distribution directly against the live
  wiki API (not a sample — every row of all 157 allowlisted commodities'
  price entries, 2004 rows total): **every single row** has *exactly one*
  of `price_buy`/`price_sell` equal to `0` and the other strictly
  positive — 1598 rows (79.7%) with `price_buy == 0`, 406 with `price_sell
  == 0`, **zero** rows with both zero, **zero** rows with both positive.
  That distribution is only explainable by "0 means this terminal doesn't
  trade the commodity in this direction" (a buy-only or sell-only
  terminal still gets a price entry for every commodity, just with the
  untraded direction reported as a literal `0` rather than the entry
  being omitted) — consistent with the in-game mechanic that a given
  terminal trades a given commodity in one direction only.
  - **Fix, at the ingestion boundary, not the client or the graph/search
    layers:** `backend/ingest/refresh.py`'s `_normalize_zero_price()`
    converts a `0`/`0.0` (or already-`None`) `price_buy`/`price_sell` to
    `None` before it's ever written to the `Price` table, applied
    independently to both directions. `backend/clients/wiki_client.py`
    stays a thin, faithful client (still reports the raw literal `0` —
    that's the API's real behavior, not a bug in the client) and
    `backend/graph/builder.py`/`search.py`'s existing missing-price
    exclusion logic needed zero changes, since a genuine `None` in the DB
    is exactly what that logic already handled correctly — the bug was
    purely that `0.0` had been reaching that logic instead of `None`.
  - The "free commodity" branch in `search.py` (`buy_price == 0 → free,
    use full cargo`) is kept, not deleted — it's provably unreachable via
    real ingested data now, but still correct, necessary defensive
    behavior for any direct caller of `find_best_route()` that hands it a
    literal `0.0` (e.g. a unit test, or a future data source with
    different semantics).
  - Verified end to end against live data, not just unit tests: before
    the fix, `POST /route` for `start_terminal_id=282`
    (Platinum Bay - Everus Harbor), `ship_id=108` (Caterpillar),
    `num_hops=4`, `starting_budget=100000` returned
    `final_cash=39744100000.0`; after the fix, the identical request
    against freshly-refreshed live data returned `final_cash=657418.0`
    (`total_profit=557418.0`) — a real, plausible multi-hop trade route
    through real curated commodities.

## Standing security ground rules

These apply to **every** task, not just the ones that mention security
explicitly. A reviewer agent should treat a violation of any of these as a
blocking finding:

- **All database access via SQLAlchemy ORM/parameterized queries only** —
  never raw string-built or concatenated SQL, anywhere, in any task.
- **No shell/subprocess use anywhere** in this app. There is no legitimate
  reason for it here, and it must stay that way — no command injection
  surface to begin with.
- **All user-facing input (query params, request bodies) must be validated
  server-side with explicit bounds**, regardless of what the frontend UI
  enforces. Pydantic models rejecting malformed types by construction is
  not enough on its own — e.g. `start_terminal_id` and (Phase 2) `ship_id`
  must be checked against real known terminals/ships (404, not a raw lookup
  failure, if either doesn't exist); (Phase 2) `num_hops` and
  `starting_budget` must be positive/non-negative and capped at a
  configured sane maximum (`settings.max_hops_cap`,
  `settings.max_starting_budget_cap`) enforced server-side. A raw API
  request can always skip the UI entirely.
- **Never use `unsafe_allow_html=True` in Streamlit** on any data sourced
  from the external APIs — terminal/commodity names come from external,
  crowd-sourced data and must be treated as untrusted display text, not
  markup.
- **Never interpolate raw user input directly into log messages** (avoids
  log injection/forging). Use lazy `%`-style logging args, and if you must
  include user-supplied values, treat them as data, not part of the format
  string.
- **Never leak internal exception details/stack traces in API error
  responses.** A generic exception handler must return a safe, generic
  message on unexpected errors — never a raw stack trace or exception
  string to the client.

Additional standing rules from the design plan, relevant once the
corresponding tasks build them:
- Rate limiting (`slowapi`) on every endpoint, tighter limits on `/refresh`
  and `/route`.
- `/refresh` gets an optional shared-secret header check (`X-Refresh-Token`
  vs `settings.refresh_token`); disabled only when `refresh_token` is unset
  (local dev default).
- CORS locked to the actual frontend origin (`settings.cors_allowed_origin`),
  never `*`.
- SQLite opened in **WAL mode** so a background/scheduled refresh writer
  and `/route` readers don't block each other with "database is locked."
- Composite indexes: `prices(terminal_id, commodity_id)`,
  `distances(terminal_a_id, terminal_b_id)`.
- Graph build and ingestion must bulk-load (one query each, not per-row/
  per-terminal loops — no N+1 queries).

## Conventions

- **Pinned dependencies only.** `requirements.txt` contains exact versions
  (from `pip freeze` against the project's `.venv`), including transitive
  dependencies, for reproducibility. Do not add an unpinned dependency —
  install it into `.venv`, then re-run `pip freeze > requirements.txt` to
  refresh the whole file.
- **Structured logging via `backend/logging_config.py`, no `print()`**
  anywhere in the codebase, in any task. Call `setup_logging()` once at
  each entrypoint (`backend/main.py`, `scripts/refresh_data.py`) before
  anything else logs. Use `logging.getLogger(__name__)` per module.
- **One commit per completed-and-reviewed task.** Commits are made by the
  orchestrator after a task passes review, not by implementer agents.
  Implementer agents should leave changes staged/unstaged for review, not
  commit them.
- **Settings access:** always via `backend.config.get_settings()` (a cached
  singleton), never by instantiating `Settings()` directly outside of
  tests, so the whole process shares one parsed config.
