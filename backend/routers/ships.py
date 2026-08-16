"""`GET /ships` -- lists every real player spaceship known to the cache DB
(e.g. for a future frontend ship selector, Phase 2).

Served directly from the cache DB via `session_scope()`, one bulk `SELECT`
-- deliberately *not* via a `GraphCache`-style in-memory singleton the way
`/terminals` is. `GraphCache` exists because the graph is expensive to
rebuild (bulk-loading prices/distances and computing edge weights across
the whole terminal set) and is on the hot path for every `/route` call, so
CLAUDE.md requires it be built once per refresh, not once per request.
Ships have neither property: the `Ship` table is small (low hundreds of
rows at most -- one row per real spaceship in the game), changes only once
per `/refresh` the same as everything else, and nothing here does per-row
work in a loop (`session.execute(select(Ship))` is one query, bulk-loaded
into a list) -- so a direct query is simplest and satisfies CLAUDE.md's
"no N+1 queries" rule without needing a second cache/lifecycle wrapper to
keep in sync with `GraphCache`'s own rebuild/atomic-swap machinery. If
`/ships` ever became a hot path or the table grew large enough for the
query itself to matter, a `ShipsCache` analogous to `GraphCache` would be
the natural next step -- not needed today.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from backend.config import get_settings
from backend.models.db import Ship, session_scope
from backend.models.schemas import ShipOut
from backend.rate_limit import limiter

router = APIRouter()


@router.get("/ships", response_model=list[ShipOut])
@limiter.limit(get_settings().rate_limit_default)
def list_ships(request: Request) -> list[ShipOut]:
    with session_scope() as session:
        ships = session.execute(select(Ship)).scalars().all()
        out = [ShipOut.model_validate(ship) for ship in ships]
    out.sort(key=lambda ship: ship.name)
    return out
