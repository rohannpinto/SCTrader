# CLAUDE.md — Star Citizen Trading Route Optimizer

This file is the **single source of shared context** for every implementer
and reviewer agent working on this project, task by task. Each agent reads
only this file plus its own task-specific instructions — not the original
design plan. If something you need isn't here, that's a gap worth flagging,
not a reason to guess.

## Project summary

A local, all-Python full-stack web app that computes efficient Star Citizen
commodity trading routes: given a starting station/terminal and a max travel
distance budget, it finds the walk through the in-game trade network that
maximizes accumulated profit, using live commodity price data pulled from
public APIs. Backend is **FastAPI** (Python 3.11+) with **SQLite** (via
SQLAlchemy) as a local on-disk cache of API data, and an in-memory
**networkx `DiGraph`** built from that cache. Frontend is **Streamlit**,
talking to the backend over local HTTP only. No auth, no deployment concerns
right now — this all runs on one machine. There is a real, tentative future
plan to deploy this to a public AWS EC2 instance; application-level security
hardening is being built now in anticipation of that (see "Security ground
rules" below), but infrastructure-level protections (WAF, ALB, Shield, TLS,
Secrets Manager, CloudWatch) are explicitly out of scope until that move
actually happens.

## Data model & edge weight formula

- **Node** = a single trade-capable terminal (not a whole station — a
  station can host multiple terminals with different prices). Non-commodity
  terminals (ship dealers, refuel-only, etc.) are filtered out during
  ingestion.
- **Edge a→b** exists only if `distance(a, b) <= DISTANCE_THRESHOLD`
  (configurable; edges above the threshold are simply not created).
- **Edge weight — the blended model (confirmed, do not change):**

  ```
  weight(a→b) = max over all commodities c of:
      max(0, sell_price(c, b) - buy_price(c, a)) / distance(a, b)
  ```

  The single blended graph picks the best commodity independently per edge
  (not one graph per commodity). Store which commodity achieved the max as
  edge metadata (`best_commodity`) so a result route can say "buy Laranite
  here, sell it there."
  - A commodity only participates in that `max` if it has **both** a valid
    buy price at `a` **and** a valid sell price at `b`. A **missing** price
    is *excluded* from consideration, never treated as `0` — `0` means
    "known and unprofitable," missing means "not traded there at all."
  - If no commodity is profitable on an edge, `weight = 0` (the edge still
    exists and still costs distance budget — occasionally useful as a
    bridge hop).
  - **`MIN_DISTANCE` floor:** two terminals at the same physical station can
    be ~0 in-game distance apart. Divide-by-zero is prevented by flooring
    the denominator at `min_distance_floor` (from config), never dividing by
    a raw distance that could be zero.

## Route search problem

This is **not** vanilla Dijkstra, and must not be implemented as a
flipped-comparator Dijkstra (that would loop forever exploiting a
positive-weight cycle). The actual problem: **starting from a fixed node,
find the walk (revisits/cycles allowed) that maximizes total accumulated
edge weight, subject to total distance travelled ≤ a user-chosen budget.**
This is a resource-constrained longest-path problem, solved with a
**label-setting search** over states `(node, distance_used)`:

- A *label* is `(current_node, distance_used_so_far, cumulative_weight, path)`.
- Explore with a max-priority queue ordered by `cumulative_weight`
  (best-first).
- From a label at `n`, for each outgoing edge `n→m` with
  `distance_used + d(n,m) <= budget`, push a new label at `m`.
- **Dominance pruning:** at a given node, label `A` dominates label `B` if
  `A.distance_used <= B.distance_used` and
  `A.cumulative_weight >= B.cumulative_weight` — drop dominated labels. This
  keeps the frontier tractable.
- Termination is guaranteed because every edge has strictly positive
  distance, so `distance_used` strictly increases each hop — the budget
  bounds the number of hops even though the underlying graph has cycles.
- Best answer = highest-`cumulative_weight` label seen across all nodes when
  the queue empties.
- **Anytime behavior / safety valves:** a configurable cap on labels
  retained per node (`search_label_cap_per_node`, keep top-K by weight) and
  a wall-clock/iteration budget (`search_time_budget_seconds`) bound
  worst-case latency on a dense graph. Because the search is best-first, the
  best label found so far when the cap/timeout hits is always a valid,
  reasonable answer — return "best found within budget," never fail or hang.
- **Isolated start node:** if the chosen start terminal has zero viable
  outgoing edges under the current distance threshold, return a clean "no
  profitable route found from here" result — not an exception, not an empty
  crash.

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
   - A small **LRU cache on `/route` results**, keyed on
     `(start_terminal_id, max_distance, distance_threshold,
     cache_data_version)`, so repeated/lightly-tweaked queries against the
     same refreshed dataset return instantly. The data-version component
     makes it self-invalidating on every refresh.

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
      builder.py                # cache DB -> networkx DiGraph with weights (pure, bulk-loaded)
      search.py                  # label-setting constrained max-weight search
      cache.py                    # GraphCache singleton: build-once-per-refresh, atomic swap
    routers/
      terminals.py                # GET /terminals
      refresh.py                   # POST /refresh, GET /refresh-status
      route.py                      # POST /route (LRU-cached by query + data version)
  frontend/
    app.py                    # Streamlit UI, calls backend over localhost
  tests/
    fixtures/                 # recorded JSON responses from both APIs
    unit/                     # weight formula, graph builder, search algorithm
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
  not enough on its own — e.g. `start_terminal_id` must be checked against
  real known terminals (404, not a raw lookup failure, if it doesn't
  exist); `max_distance` and `distance_threshold` must be positive and
  capped at a configured sane maximum enforced server-side. A raw API
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
