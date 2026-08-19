---
title: Star Citizen Trading Route Optimizer
emoji: 🚀
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
---

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

## Deployment: Hugging Face Spaces (Docker)

This repo can be deployed for free as a [Hugging Face
Space](https://huggingface.co/docs/hub/spaces) using its Docker SDK. Hugging
Face's free tier only really supports one exposed process per Space, so
rather than host the backend and frontend as two separate free services
(each independently cold-starting), this packages both into **one Docker
container**: FastAPI and Streamlit run together, with Streamlit (the only
externally-exposed port) talking to FastAPI over `localhost` inside the
container -- exactly how they already talk to each other in local dev, per
the "Running" section above. This requires **zero application code
changes** -- only three new files:

- `Dockerfile` (project root) -- builds the image, installs
  `requirements.txt`, copies `backend/`, `frontend/`, and `scripts/` in,
  and runs the startup script below as its `CMD`.
- `scripts/start_hf_space.sh` -- the container's entrypoint. In order: runs
  `python scripts/refresh_data.py` once synchronously (so a fresh
  container never serves an empty app before the first scheduled
  auto-refresh), starts `uvicorn backend.main:app` in the background,
  polls `GET /health` until the backend is ready (bounded, not a blind
  sleep), then `exec`s `streamlit run frontend/app.py` in the foreground
  as the container's main process.
- This `README.md`'s own YAML frontmatter above (`sdk: docker`,
  `app_port: 7860`) -- Hugging Face Spaces reads a Space's configuration
  directly from this block.

### How to deploy

1. Create a new Space at [huggingface.co/new-space](https://huggingface.co/new-space),
   choosing **Docker** as the SDK (any Docker template, e.g. "Blank", is
   fine -- this repo's own `Dockerfile` is what actually gets built).
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
   automatically. Watch the Space's "Logs" tab for `start_hf_space.sh`'s
   startup output (initial refresh, backend readiness, then Streamlit
   coming up) -- first boot takes a few minutes (dependency install +
   the initial data refresh's external API calls), not the usual few
   seconds of an already-warm Space.

### Two things worth knowing about the free tier

- **Sleep and cold starts.** Per Hugging Face's own stated free-tier
  policy, a Space sleeps after 48 hours with no visits, and the next
  visitor triggers a cold start (roughly 30-90 seconds) while the
  container comes back up. Separately, the Space's disk is **ephemeral**:
  it's wiped on a rebuild/redeploy (a new commit pushed, or a manual
  "Factory reboot"), but *not necessarily* on an ordinary sleep/wake cycle
  -- those are two different events with two different disk-persistence
  behaviors, worth not conflating. Either way, this app tolerates a wiped
  disk gracefully: `start_hf_space.sh`'s synchronous initial refresh (see
  above) repopulates the cache DB from scratch on every fresh boot, so a
  visitor never sees a permanently empty app -- just the normal cold-start
  wait, plus however long that one refresh takes.
- **`/refresh` exposure.** In this single-container design, the FastAPI
  backend (port 8000, including `/refresh`) is **never reachable from the
  public internet at all** -- Hugging Face Spaces only routes external
  traffic to the one `app_port` declared in the YAML frontmatter (`7860`,
  Streamlit's port); port 8000 only exists inside the container, and the
  only thing that ever calls it is Streamlit's own Python process, over
  `localhost`. That gives the entire backend API surface real protection
  "for free," on top of whatever the app's own auth/rate-limiting already
  provides. The one caveat: the "Refresh data now" button *inside* the
  Streamlit UI itself **is** reachable by any visitor to a public Space --
  there's no visitor authentication at that layer. This is already
  mitigated by the app's existing refresh-overlap lock (a second refresh
  while one is running gets rejected, never runs concurrently) and its
  rate limiting on `/refresh`. As an **optional** extra layer, not a
  required one, the project owner can still set a `REFRESH_TOKEN` value
  via the Space's own **Secrets** feature (an environment-variable secret
  store built into every Hugging Face Space, documented on their site) if
  further defense in depth is wanted -- nothing about that requires any
  change to this repo's code or this deployment setup; it's purely a
  config choice left to the project owner.
