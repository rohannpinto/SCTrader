# Star Citizen Trading Route Optimizer

A local, all-Python web app that computes efficient Star Citizen commodity
trading routes: given a starting station and a max travel distance, it finds
the walk through the trade network that maximizes accumulated profit, using
live commodity price data pulled from `api.star-citizen.wiki` and UEX Corp.

Backend is FastAPI + SQLite (a local on-disk cache of the two APIs) + an
in-memory `networkx` graph rebuilt after every data refresh; frontend is
Streamlit, talking to the backend over local HTTP only. No auth, no
deployment concerns right now -- everything here runs on one machine. See
`CLAUDE.md` for the full architecture, data model, edge-weight formula,
route-search algorithm, and security ground rules.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and adjust values as needed -- every setting
has a working default, so this is optional for local use. `.env` is
gitignored and loaded automatically by `backend/config.py`.

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

The backend pre-warms its in-memory graph from whatever's already in
`data/cache.db` on startup, but that cache starts out empty on a fresh
checkout. Populate it once before searching for routes, either:

```powershell
# One-off, from the command line -- doesn't need the backend running:
python scripts/refresh_data.py
```

or, with the backend already running, `POST http://localhost:8000/refresh`
(add an `X-Refresh-Token` header if `REFRESH_TOKEN` is set in `.env`). Then
open the Streamlit URL it prints (default `http://localhost:8501`) to pick a
starting terminal and search for a route. `GET /refresh-status` reports the
most recent refresh's outcome, row counts, and any warnings.

A refresh takes a little while (it calls both external APIs); `/refresh` is
rate-limited and rejects a second concurrent call with `409` while one is
already running.

## Testing

```powershell
pytest                    # default suite: fast, fully offline, respx-mocked
pytest -m live             # opt-in: hits the real external APIs once, checks for schema drift
pytest -m perf              # opt-in: synthetic large-graph performance benchmark
pytest -m "live or perf"     # both opt-in groups explicitly
```

The default `pytest` run is what CI/a pre-commit check should use -- it
never touches the network and finishes in well under a minute. `-m live`
is a manual sanity check against the real wiki/UEX APIs (schema drift,
endpoint availability); `-m perf` is a synthetic-data benchmark, not a
correctness check, and its timing bounds are deliberately generous so it
stays reliable on a slow machine.
