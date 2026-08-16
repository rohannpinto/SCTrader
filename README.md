# Star Citizen Trading Route Optimizer

A local, all-Python web app that computes efficient Star Citizen commodity
trading routes: given a starting terminal, a selected ship (its quantum
drive range and cargo capacity), a hop-count budget, and a starting cash
balance, it finds the walk through the trade network that maximizes final
cash on hand, using live commodity price data pulled from
`api.star-citizen.wiki` and UEX Corp. Only a curated allowlist of real
tradeable materials/food/organics/vice commodities is considered --
seasonal/cosmetic/placeholder catalog entries (e.g. event gifts, ship
ammo, engine placeholders) are excluded during ingestion.

Backend is FastAPI + SQLite (a local on-disk cache of the two APIs) + an
in-memory `networkx` graph rebuilt after every data refresh; frontend is
Streamlit, talking to the backend over local HTTP only. No auth, no
deployment concerns right now -- everything here runs on one machine. See
`CLAUDE.md` for the full architecture, data model, hop-count/cash/cargo
search algorithm, and security ground rules.

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
(add an `X-Refresh-Token` header if `REFRESH_TOKEN` is set in `.env`). This
populates terminals, curated commodities, prices, distances, and real
player ships (quantum drive range + cargo capacity, from
`api.star-citizen.wiki`'s `/vehicles` data). Then open the Streamlit URL it
prints (default `http://localhost:8501`) to pick a starting terminal
(searchable, filterable by star system and planetoid), a ship, a hop-count
budget, and a starting cash balance, and search for a route -- the
selected ship's quantum range gates which hops are reachable, and its
cargo capacity caps how much of a commodity can be traded per hop.
`GET /refresh-status` reports the most recent refresh's outcome, row
counts (including ships), and any warnings. `GET /ships` and `GET
/terminals` list what's currently available to search over.

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
