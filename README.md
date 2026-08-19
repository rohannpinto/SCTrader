# Star Citizen Trading Route Optimizer

**Live: [sctrader.onrender.com](https://sctrader.onrender.com)**
· [LinkedIn](https://www.linkedin.com/in/rohan-n-pinto/)
· [GitHub](https://github.com/rohannpinto)

Given a starting terminal, a ship, a hop budget, and a starting cash
balance, this finds the sequence of buys and sells through *Star Citizen*'s
in-game trade network that ends with the most money — using live commodity
prices pulled from two public APIs.

The interesting part isn't the web app. It's that "most profitable trade
route" turns out to be a resource-constrained optimization problem on a
cyclic directed graph that **standard shortest-path algorithms cannot
solve**, and the search here is provably optimal rather than greedy. That
argument is written out in [The routing problem](#the-routing-problem)
below.

> **Note on the free host:** the live site sleeps after 15 minutes of
> inactivity, so the first request after a quiet period takes about a
> minute to cold-start. It isn't broken; it's waking up.

---

## What it does

- Pulls live terminal, commodity, price, distance, and ship data from
  `api.star-citizen.wiki` and UEX Corp into a local SQLite cache.
- Builds an in-memory `networkx` directed graph — currently **161
  trade-capable terminals and ~24,900 directed edges** — rebuilt once per
  data refresh, never per request.
- Searches that graph for the walk (revisits and cycles allowed) that
  maximizes final cash, respecting the selected ship's quantum-drive range
  and cargo capacity and the user's cash on hand at every step.
- Reports the route stop by stop: what to buy where, what to sell where,
  how much of it, and the running cash balance.
- Tells you **how stale the underlying prices are**, because that
  determines whether any of the above is worth acting on.

---

## The routing problem

### Why this isn't Dijkstra

The obvious framing — "weight each edge by profit and find the best path" —
fails on three counts:

1. **We want to *maximize*, and cycles are legal.** Bouncing between two
   terminals that trade profitably in both directions is a genuinely good
   strategy in this game. A shortest-path algorithm with a flipped
   comparator doesn't just give a wrong answer here; it **never
   terminates**, because it can always improve by going around a
   positive-weight cycle one more time.
2. **Edge "profit" isn't a property of the edge.** How much you earn
   crossing `a → b` depends on how much cash you have *when you arrive at
   `a`* — with 5,000 aUEC you can only afford a few units of an expensive
   commodity, so a cheaper one with a thinner per-unit margin can win
   outright. The profit-maximizing commodity for an edge is a
   search-time fact, not a graph-build-time one, so there is no static
   weight to precompute.
3. **The budget is a second dimension.** Every hop costs one hop from a
   fixed allowance, independent of profit, so this is a *resource-
   constrained* longest-walk problem — closer to a bounded knapsack over a
   graph than to shortest paths.

### The model

- **Node** = one trade-capable terminal. Not a station: a single station
  can host several terminals with different prices.
- **Edge `a → b`** = a known directed distance. The graph is *not*
  pre-filtered by distance; traversability is decided at search time by
  whether `edge.distance <= ship_quantum_range`, so the same cached graph
  serves every ship.
- **Per-hop decision.** For edge `a → b` with cash `c` on hand, consider
  every commodity with both a valid buy price at `a` and a valid sell price
  at `b` (a set intersection, not a scan of the whole catalog). For each:

  ```
  quantity = min(floor(c / buy_price), ship_cargo_capacity)
  profit   = quantity * (sell_price - buy_price)      # only if sell > buy
  ```

  The commodity maximizing **total** profit wins — not the best per-unit
  margin. If nothing is profitable, the hop is a neutral "bridge" hop:
  it still costs a hop, and is sometimes worth it to reach a good edge
  further along.

### The search: a bounded DP, and why one label per state is enough

State is `(node, hops_used, cash)`. The search is a plain dynamic program
over `hop = 1 … num_hops`: for every labelled state at `hop - 1`, try every
in-range outgoing edge and keep the best result per `(node, hop)`.

The load-bearing claim is that keeping only the **single highest-cash label
per `(node, hop)`** loses nothing:

> **Claim.** At a fixed `(node, hops_used)`, more cash weakly dominates
> less cash.
>
> **Why.** Take labels `A` and `B` at the same state with
> `cash(A) >= cash(B)`, and any identical future edge sequence. At each
> step, `quantity = min(floor(cash / buy_price), cargo_cap)` is
> monotonically non-decreasing in `cash`, and it is capped by the *same
> fixed* `cargo_cap` regardless of which label is being extended. So `A`
> can replicate every purchase `B` makes and afford at least as much of it,
> leaving `A` with at least as much cash after every subsequent hop. `B`
> can therefore never overtake `A`. ∎

This is what makes the problem tractable: there's no second axis to trade
off, so no Pareto frontier, no priority queue, and no per-node label cap.
The state space is exactly `O(nodes × num_hops)` — bounded by construction
rather than by a heuristic cutoff.

Termination is *structural*, not merely resource-bounded: the DP is a fixed
loop over hop counts, so it cannot re-queue a profitable cycle forever. A
cycle just shows up as revisiting a node at a later hop with more cash.

**Path reconstruction uses parent pointers**, not accumulated path tuples.
An earlier version concatenated a new tuple onto a path at every hop,
which made label creation itself `O(depth)` and left many long-lived labels
each holding an independent long tuple — the post-search reference-counting
teardown dominated wall time. Every label is now `O(1)` to create, and only
the single winning label ever walks its chain back to the start.

### Kth-best routes: the same proof, one level up

The app has a risk/reward slider, because the *most* profitable route is
also the one most likely to have already been flown by someone else before
the price data even reached us. Lowering it asks for the Kth-best distinct
route instead.

Getting there greedily — "take the 2nd-best edge at each step" — would
throw away optimality entirely, since a greedy walk can't look ahead the
way the DP does. Instead the DP retains the **top-K labels per state**,
sorted by cash. The correctness argument is the dominance proof applied one
level up:

> If a label falls outside the top-K retained at some state, it cannot
> produce a route in the true global top-K. Each of the K labels ranked
> above it, extended by that same future edge sequence, finishes with at
> least as much cash — giving K distinct routes at least as good as
> anything the discarded label could reach.

So top-K pruning is exact, not a heuristic. `K = 1` dispatches to the
original single-label DP, so the default path costs exactly what it did
before.

### Complexity and measured cost

| | |
|---|---|
| States | `O(V × H)` — `V` terminals, `H` hop budget |
| Labels retained | `O(V × H × K)` for the Kth-best variant |
| Per-label work | one pass over in-range out-edges × the buy/sell commodity intersection |

Measured on a synthetic 300-terminal / ~18,000-edge dataset (larger than
the real game data): graph build **0.13–0.16 s**, bulk-loaded in three
queries with no N+1. A wall-clock safety valve exists in the search but is
not expected to fire — it's checked only between fully-completed hop
levels, so a firing deadline always leaves the DP table consistent enough
to read the best answer out of.

---

## Data quality: two bugs worth reading about

Both produced *confidently wrong* output rather than crashes, which is what
makes them worth writing down.

**A $39.7 billion phantom route.** A live search returned a route earning
39.7 billion aUEC. The cause: the wiki API reports a price of `0` for a
commodity a terminal doesn't trade *in that direction*, and the search read
`buy_price == 0` as "free — fill the entire hold." I checked the full
distribution rather than patching the symptom: across all 2,004 real price
rows, **every single row has exactly one of `price_buy`/`price_sell` equal
to zero and the other strictly positive** — never both, never neither. That
distribution is only explainable as "zero means not traded in this
direction." Fixed at the ingestion boundary by normalizing those zeros to
`NULL`, so "missing" and "genuinely zero" stay distinguishable everywhere
downstream. The same live query then returned 657,418 aUEC — a real route.

**Trading Luminalia Gifts for infinite money.** Before that, the search
found a loop trading a seasonal holiday gift item. The commodity catalog
includes non-tradeable entries — event gifts, ship ammunition, cosmetics,
and engine placeholders like `Heat` and `Life Support`. Ingestion now
applies a curated allowlist over the API's own `commodity_groups` tags,
verified against all 206 live commodities: **157 pass, 49 excluded.** The
rule is OR-semantics across groups, which matters — a strict AND would have
wrongly dropped every refined drug and synthetic material in the game,
since those carry a legitimate tag *and* the mixed-bag `ProcessedGoods` tag.

**And one honesty problem.** With an hourly auto-refresh running, the UI's
"last refreshed: minutes ago" read as "this data is current." It isn't —
prices are crowd-sourced player sightings. Measured across the real
dataset: **median report age ~7 days, and not one row under 24 hours old.**
The app now surfaces that distribution directly, with a fresh-to-rotten
meter, so a visitor can judge whether to trust it at all. That gap is also
the entire reason the risk/reward slider exists.

---

## Architecture

```
backend/
  clients/      wiki + UEX API clients (retry/backoff, pagination)
  ingest/       API -> SQLite cache: ID joining, curation, zero-price normalization
  graph/        builder (cache -> DiGraph) | search (the DP) | cache (singleton)
  routers/      /terminals /ships /route /refresh /refresh-status
  models/       SQLAlchemy tables + Pydantic request/response schemas
frontend/       Streamlit UI (server-side HTTP to the backend only)
tests/          unit | integration | perf | recorded API fixtures
```

**Two-tier caching.** SQLite on disk is the source of truth; an in-memory
`GraphCache` singleton holds the graph and the bulk-loaded price indices.
`rebuild()` constructs a whole new snapshot and swaps the reference in one
step, so an in-flight request sees either the fully-old or fully-new graph
— never a half-populated one, and no locking on the read path. A small LRU
cache sits on `/route` results, keyed on the search parameters plus a data
version that changes on every refresh, making it self-invalidating.

**Refresh safety.** An in-process lock means a manual refresh and a
scheduled one can never run concurrently (the second gets a `409`), so the
external APIs are never double-hit. SQLite runs in WAL mode so the refresh
writer and route readers don't block each other.

**Security.** ORM/parameterized queries only, no raw SQL; no
shell/subprocess anywhere; all input bounds-checked server-side
independently of what the UI allows; rate limiting on every endpoint;
generic error responses that never leak internals. Terminal and commodity
names are untrusted crowd-sourced strings and are never rendered as markup
— enforced by an AST-based test that fails if any `unsafe_allow_html` call
site receives anything but a static literal.

**296 tests**, including hand-computed algorithm scenarios, a brute-force
cross-check of the optimality argument, adversarial input cases, and
opt-in live-API and performance suites.

---

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and adjust as needed — every setting has a
working default, so this is optional for local use. `.env` is gitignored
and loaded automatically by `backend/config.py`.

## Running

Two processes, each in its own terminal (venv activated in both):

```powershell
# Terminal 1 -- backend (from the project root, so `backend` is importable)
uvicorn backend.main:app --reload
```

```powershell
# Terminal 2 -- frontend
streamlit run frontend/app.py
```

The cache starts empty on a fresh checkout. Populate it once:

```powershell
python scripts/refresh_data.py     # doesn't need the backend running
```

or `POST http://localhost:8000/refresh` with the backend up (add an
`X-Refresh-Token` header if `REFRESH_TOKEN` is set). Then open the
Streamlit URL it prints (default `http://localhost:8501`).

A refresh takes a little while — it calls both external APIs — and is
rate-limited; a second concurrent call gets `409`.

## Testing

```powershell
pytest                    # default: fast, fully offline, respx-mocked
pytest -m live             # opt-in: hits the real APIs once, checks for schema drift
pytest -m perf              # opt-in: synthetic large-graph benchmark
```

The default run never touches the network and finishes in a couple of
minutes. `-m live` is a manual sanity check against the real wiki/UEX APIs;
`-m perf` is a benchmark rather than a correctness check, with deliberately
generous bounds so it stays reliable on a slow machine.

## Deployment: Render (Docker, free tier)

This is the current recommended free option. This repo can be deployed for
free as a [Render](https://render.com) **Web Service** using its Docker
runtime -- no credit card required, and Render explicitly supports deploying
an existing `Dockerfile` from a connected GitHub repo. (This section
replaces Hugging Face Spaces as "the free option" -- see the note at the top
of the next section for why.)

Same single-container design as the Hugging Face Spaces section further
below also uses: FastAPI and Streamlit run together in **one Docker
container**, with Streamlit (the only externally-routed port) talking to
FastAPI over `localhost` inside the container -- exactly how they already
talk to each other in local dev, per the "Running" section above. This
requires **zero application code changes** -- the same `Dockerfile` and
`scripts/start_web_container.sh` used for that path work here unmodified,
because the startup script reads its listen port from the `$PORT`
environment variable (falling back to `7860` if unset) rather than
hardcoding one -- and Render is the platform that
actually needs that: it injects `$PORT` into the container automatically
(defaulting to `10000` if you don't override it in the dashboard), and your
service **must** bind to it on `0.0.0.0`, which `start_web_container.sh`
now does.

### How to deploy

1. Push this repo to GitHub (Render deploys from a connected Git repo, not
   a `git push` to a Render-hosted remote).
2. Create a new **Web Service** at [Render's dashboard](https://dashboard.render.com/),
   connect the GitHub repo, and choose **Docker** as the runtime/environment
   -- Render auto-detects the root `Dockerfile`; if it doesn't, point it at
   the repo root manually. No build/start command overrides are needed --
   the `Dockerfile`'s own `CMD` (`scripts/start_web_container.sh`) is used
   as-is.
3. Leave `PORT` unset in Render's environment variable settings -- Render
   injects it itself. Render builds the image and starts the container
   automatically; watch the service's "Logs" tab for
   `start_web_container.sh`'s startup output (initial refresh, backend
   readiness, then Streamlit coming up) -- first boot takes a few minutes
   (dependency install + the initial data refresh's external API calls),
   not the usual few seconds of an already-warm instance.

### Things worth knowing about the free tier

(Verified directly against Render's own current docs, not carried over
from the Hugging Face research below -- the two platforms' free tiers work
differently.)

- **750 free instance-hours per month**, no credit card required to sign up
  or to create a free web service.
- **Spin-down and cold starts.** A free Render web service spins down after
  **15 minutes of inactivity**, and the next request triggers a cold start
  -- roughly **about a minute** while the container comes back up (slower
  than Hugging Face's own cold-start window, and a much shorter idle
  timeout before it triggers). Render's disk is also **ephemeral** across a
  spin-down/restart or a redeploy, same as Hugging Face's. This app already
  tolerates that gracefully: `start_web_container.sh`'s synchronous initial
  refresh (see below) repopulates the cache DB from scratch on every fresh
  boot, so a visitor never sees a permanently empty app -- just the normal
  cold-start wait, plus however long that one refresh takes.
- **Bind the backend to loopback, not `0.0.0.0` -- this one bit us.**
  Render decides which port to route your public URL to by **scanning for
  open ports inside the container**. With the backend listening on
  `0.0.0.0:8000` alongside Streamlit on `$PORT`, Render saw *two* open
  ports, and the deployed URL intermittently returned a bare **"Not
  Found"** -- traffic landing on FastAPI's undefined `/` route. Render's
  own logs gave it away: `Detected a new open port HTTP:8000` appearing
  *after* the service was already declared live on the right port. The fix
  is one flag: `start_web_container.sh` starts uvicorn on `127.0.0.1:8000`,
  so only Streamlit's port is visible to the scanner. Note that verifying
  a port is merely un-*published* by Docker (`docker port`, `-p` flags)
  does **not** catch this -- Render never consults Docker's port
  publishing.
- **`/refresh` exposure.** Same architecture, same conclusion as the
  Hugging Face section below, just a different platform doing the routing:
  in this single-container design, the FastAPI backend (port 8000,
  including `/refresh`) is **never reachable from the public internet at
  all** -- it is bound to loopback (see above), so it exists only inside
  the container, and the only thing that ever calls it is Streamlit's own
  Python process, over `localhost`. That gives the entire
  backend API surface real protection "for free," on top of whatever the
  app's own auth/rate-limiting already provides. The one caveat, unchanged
  from the Hugging Face case: the "Refresh data now" button *inside* the
  Streamlit UI itself **is** reachable by any visitor to a public service
  -- there's no visitor authentication at that layer. This is already
  mitigated by the app's existing refresh-overlap lock (a second refresh
  while one is running gets rejected, never runs concurrently) and its
  rate limiting on `/refresh`. As an **optional** extra layer, not a
  required one, the project owner can still set a `REFRESH_TOKEN` value
  via Render's own **Environment Variables** dashboard section if further
  defense in depth is wanted -- nothing about that requires any change to
  this repo's code or this deployment setup; it's purely a config choice
  left to the project owner.

## Deployment: Hugging Face Spaces (Docker)

**No longer a free option, as of a Hugging Face policy change in July
2026** -- this section originally described Hugging Face Spaces' Docker SDK
as a free deployment target, and that was accurate when it was written, but
Hugging Face now requires a paid **PRO plan** to create a new Docker (or
Gradio) Space; only **Static Spaces** (which cannot run a Python backend at
all) remain free there. See the "Deployment: Render (Docker, free tier)"
section above for the current free recommendation. The instructions below
are kept, unchanged in substance, for anyone who already has (or gets) a
Hugging Face PRO plan and specifically wants to deploy there instead --
they are **not** presented as a free path anymore.

Hugging Face Spaces' Docker SDK only really supports one exposed process
per Space, so rather than host the backend and frontend as two separate
services (each independently cold-starting), this packages both into **one
Docker container**: FastAPI and Streamlit run together, with Streamlit (the
only externally-exposed port) talking to FastAPI over `localhost` inside
the container -- exactly how they already talk to each other in local dev,
and exactly the same single-container design the Render section above
reuses. This requires **zero application code changes** -- only three new
files, shared with the Render deployment above:

- `Dockerfile` (project root) -- builds the image, installs
  `requirements.txt`, copies `backend/`, `frontend/`, and `scripts/` in,
  and runs the startup script below as its `CMD`.
- `scripts/start_web_container.sh` -- the container's entrypoint. In order:
  runs `python scripts/refresh_data.py` once synchronously (so a fresh
  container never serves an empty app before the first scheduled
  auto-refresh), starts `uvicorn backend.main:app` in the background,
  polls `GET /health` until the backend is ready (bounded, not a blind
  sleep), then `exec`s `streamlit run frontend/app.py` in the foreground
  as the container's main process, bound to the port named by `$PORT`
  (falling back to `7860` if unset -- Hugging Face Spaces doesn't inject
  `$PORT` the way Render does, so this fallback is what actually gets used
  there).
- A Hugging Face Space's **own** `README.md` needs a YAML frontmatter block
  at its very top declaring its configuration -- this is a Hugging-Face-
  Spaces-specific requirement, **not** something GitHub or any standard
  Git tooling looks for, so it is deliberately **not** included at the top
  of *this* repo's `README.md`. If you create a Space for this repo, add
  this block to the Space's `README.md` (or fill in the equivalent fields
  in the Space's web UI, which does the same thing):

  ```yaml
  ---
  title: Star Citizen Trading Route Optimizer
  emoji: 🚀
  colorFrom: indigo
  colorTo: blue
  sdk: docker
  app_port: 7860
  ---
  ```

### How to deploy

1. Create a new Space at [huggingface.co/new-space](https://huggingface.co/new-space)
   (requires a PRO plan to select **Docker** as the SDK, per the policy
   change noted above; any Docker template, e.g. "Blank", is fine -- this
   repo's own `Dockerfile` is what actually gets built).
2. A Hugging Face Space is itself a git repository. Add it as a remote and
   push this repo to it:

   ```powershell
   git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
   git push space main
   ```

   (Requires a Hugging Face account and being logged in via `huggingface-cli
   login` or a credential-helper-cached access token -- see Hugging Face's
   own docs for git authentication specifics.)
3. Hugging Face builds the `Dockerfile` and starts the container
   automatically. Watch the Space's "Logs" tab for
   `start_web_container.sh`'s startup output (initial refresh, backend
   readiness, then Streamlit coming up) -- first boot takes a few minutes
   (dependency install + the initial data refresh's external API calls),
   not the usual few seconds of an already-warm Space.

### Two things worth knowing about the free tier

(Kept as originally researched -- still accurate for anyone actually using
this path on a PRO plan, just no longer "the free tier" in the sense of
free-to-create.)

- **Sleep and cold starts.** Per Hugging Face's own stated policy, a Space
  sleeps after 48 hours with no visits, and the next visitor triggers a
  cold start (roughly 30-90 seconds) while the container comes back up.
  Separately, the Space's disk is **ephemeral**: it's wiped on a
  rebuild/redeploy (a new commit pushed, or a manual "Factory reboot"), but
  *not necessarily* on an ordinary sleep/wake cycle -- those are two
  different events with two different disk-persistence behaviors, worth
  not conflating. Either way, this app tolerates a wiped disk gracefully:
  `start_web_container.sh`'s synchronous initial refresh (see above)
  repopulates the cache DB from scratch on every fresh boot, so a visitor
  never sees a permanently empty app -- just the normal cold-start wait,
  plus however long that one refresh takes.
- **`/refresh` exposure.** In this single-container design, the FastAPI
  backend (port 8000, including `/refresh`) is **never reachable from the
  public internet at all** -- Hugging Face Spaces only routes external
  traffic to the one `app_port` declared in the Space's YAML frontmatter
  (`7860`, Streamlit's port); port 8000 only exists inside the container,
  and the only thing that ever calls it is Streamlit's own Python process,
  over `localhost`. That gives the entire backend API surface real
  protection "for free," on top of whatever the app's own auth/rate-
  limiting already provides. The one caveat: the "Refresh data now" button
  *inside* the Streamlit UI itself **is** reachable by any visitor to a
  public Space -- there's no visitor authentication at that layer. This is
  already mitigated by the app's existing refresh-overlap lock (a second
  refresh while one is running gets rejected, never runs concurrently) and
  its rate limiting on `/refresh`. As an **optional** extra layer, not a
  required one, the project owner can still set a `REFRESH_TOKEN` value via
  the Space's own **Secrets** feature (an environment-variable secret store
  built into every Hugging Face Space, documented on their site) if further
  defense in depth is wanted -- nothing about that requires any change to
  this repo's code or this deployment setup; it's purely a config choice
  left to the project owner.
