# Star Citizen Trading Route Optimizer

**Live: [sctrader.onrender.com](https://sctrader.onrender.com)**

A full-stack Python web app that finds optimized commodity trading routes in
*Star Citizen*. You pick a starting terminal, a ship, how many jumps you want
to make, and how much cash you're starting with; it returns the sequence of
buys and sells that ends with the most money, using live price data from
`api.star-citizen.wiki` and UEX Corp.

The trade network is modelled as a weighted, directed graph (terminals are
nodes, known jump distances are edges) and searched with a custom
shortest-path-style algorithm.

> The live site is on a free tier that sleeps after 15 minutes idle, so the
> first request after a quiet period takes about a minute to wake up.

## Stack

FastAPI backend, Streamlit frontend, SQLite for the local data cache,
`networkx` for the graph, all Python 3.12.

## How the routing works

Each node is a single trade-capable terminal (a station can host several,
with different prices). Each edge carries the real in-game distance between
two terminals.

A few things rule out an off-the-shelf shortest-path algorithm:

- We're maximizing rather than minimizing, and revisiting terminals is
  legal and often optimal. A stock algorithm with a flipped comparator
  would loop forever on a profitable cycle.
- Edge weight can't be precomputed. Which commodity pays best on a given
  edge depends on how much cash you have when you get there — with a small
  balance you may only afford a few units of the high-margin option, so a
  cheaper commodity can win on total profit.
- The budget is a hop count, spent independently of profit.

So the search tracks `(terminal, hops used, cash)` and works forward one hop
at a time, keeping the best cash figure reached at each terminal for each hop
count. Per hop it picks the commodity and quantity maximizing profit:

```
quantity = min(floor(cash / buy_price), ship_cargo_capacity)
profit   = quantity * (sell_price - buy_price)
```

The ship's quantum range decides which edges are traversable at all, and its
cargo capacity caps quantity. If no commodity is profitable on an edge, the
hop is still allowed — it just repositions you.

Keeping only the best-cash entry per `(terminal, hop)` is safe: more cash at
the same terminal and hop count is never worse, since quantity only rises
with cash and is capped the same way either way. That keeps the search space
at roughly `terminals × hops`.

The risk/reward slider asks for the 2nd, 3rd, … best route instead of the
best one, on the theory that the most profitable route is also the one most
likely to have already been flown by someone else. It's implemented by
retaining the top N entries per state rather than just one.

Current real dataset: 161 trade terminals, ~24,900 edges, 157 commodities,
237 ships.

## Notes on the data

Worth knowing if you're reading the ingestion code — both of these caused
wrong-but-plausible results before they were handled:

- **A price of `0` means "not traded in this direction," not "free."** Across
  all 2,004 real price rows, exactly one of `price_buy`/`price_sell` is zero
  and the other is positive — never both, never neither. Ingestion converts
  those zeros to `NULL` so they're excluded rather than read as free goods.
- **The commodity catalog includes non-tradeable entries** — event gifts,
  ship ammunition, cosmetics, and engine placeholders like `Heat` and `Life
  Support`. Ingestion filters against an allowlist of the API's own
  `commodity_groups` tags: 157 of 206 pass.

Prices are crowd-sourced player reports, not a live feed. Median report age
is around a week, so the app shows that age prominently rather than just the
last fetch time.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` if you want to change anything — every setting
has a working default.

## Running

Two processes, each in its own terminal:

```powershell
uvicorn backend.main:app --reload     # backend, from the project root
streamlit run frontend/app.py          # frontend
```

The cache starts empty. Populate it once:

```powershell
python scripts/refresh_data.py         # doesn't need the backend running
```

or `POST http://localhost:8000/refresh` with the backend up. Then open the
Streamlit URL (default `http://localhost:8501`). The backend also
auto-refreshes hourly.

## Testing

```powershell
pytest              # default: fast, offline, respx-mocked (296 tests)
pytest -m live       # opt-in: hits the real APIs, checks for schema drift
pytest -m perf        # opt-in: synthetic large-graph benchmark
```

## Deployment

Deployed on [Render](https://render.com)'s free tier as a Docker web
service. Both processes run in one container: Streamlit is the only
externally-routed port, and it talks to FastAPI over `localhost` inside the
container, the same way they talk in local dev.

To deploy your own: create a Web Service from a connected GitHub repo,
choose Docker, and leave `PORT` unset — Render injects it and
`scripts/start_web_container.sh` reads it. No build or start command
overrides needed. First boot takes a few minutes because of the initial data
refresh.

Free-tier behavior worth knowing: 750 instance-hours/month, spins down after
15 minutes idle with roughly a minute of cold start, and the disk is
ephemeral — the startup script re-runs a full data refresh on every boot, so
a fresh container is never empty.

**One deployment gotcha.** Render picks which port to route by scanning for
open ports inside the container. With the backend on `0.0.0.0:8000` next to
Streamlit on `$PORT`, it saw two and the URL intermittently returned "Not
Found" — traffic landing on FastAPI's undefined `/`. The fix is binding the
backend to `127.0.0.1:8000` so only Streamlit's port is visible. Checking
that a port is un-published by Docker doesn't catch this; Render doesn't
consult Docker's port publishing.

## Security

ORM/parameterized queries only, no raw SQL, no shell/subprocess use. Input
is bounds-checked server-side independently of the UI, endpoints are rate
limited, and errors never return internals. Terminal and commodity names are
untrusted crowd-sourced strings and are never rendered as markup — enforced
by a test that fails if any `unsafe_allow_html` call site receives anything
but a static literal.

See `CLAUDE.md` for fuller architecture and design notes.
