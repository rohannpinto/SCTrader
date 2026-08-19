# Star Citizen Trading Route Optimizer

**Live: [sctrader.onrender.com](https://sctrader.onrender.com)**

A full-stack Python web app that finds optimized trading routes in
Star Citizen based on user selected constraints and community sourced pricing data from
`api.star-citizen.wiki` and UEX Corp.

The trade network is a weighted, directed graph data structure and queried with a forward DP search algorithm. 

## Stack

FastAPI backend, Streamlit frontend, SQLite for the local data cache,
`networkx` for the graph, all Python 3.12.

## Why not Dijkstra's? 

- We're maximizing rather than minimizing
- Loops are allowed and can be optimal a
- Edge weight can't be precomputed.

## How does the search algorithm work?

The search tracks `(terminal, hops used, cash)` and works forward one hop
at a time, keeping the best cash figure reached at each terminal for each hop
count. 

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

## Security

ORM/parameterized queries only, no raw SQL, no shell/subprocess use. Input
is bounds-checked server-side independently of the UI, endpoints are rate
limited, and errors never return internals. Terminal and commodity names are
untrusted crowd-sourced strings and are never rendered as markup — enforced
by a test that fails if any `unsafe_allow_html` call site receives anything
but a static literal.

See `CLAUDE.md` for fuller architecture and design notes.
