"""`POST /route` -- searches for the walk of at most `num_hops` hops from a
starting terminal that maximizes final cash on hand, given a selected ship's
quantum range (per-hop distance filter) and cargo capacity (per-hop quantity
cap) and a starting cash balance (Phase 2 hop-count/cash/cargo model -- see
CLAUDE.md's "Route search problem").

The actual algorithm lives entirely in `backend/graph/search.py`; this
router's job is request validation (CLAUDE.md's security ground rules --
`start_terminal_id` and `ship_id` checked against real known
terminals/ships, `num_hops`/`starting_budget` capped server-side regardless
of client input), resolving the selected ship's `quantum_range_gm`/
`cargo_capacity_scu`, a small self-invalidating cache in front of the
(not-cheap-on-a-dense-graph) search, and translating the low-level
`RouteSearchResult` into the API's `RouteResponse` shape (filling in display
names the search layer deliberately has no knowledge of).
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import networkx as nx
from fastapi import APIRouter, HTTPException, Request

from backend.config import get_settings
from backend.graph.cache import get_graph_cache
from backend.graph.search import RouteSearchResult, find_best_route
from backend.models.db import Ship, session_scope
from backend.models.schemas import RouteHop, RouteRequest, RouteResponse
from backend.rate_limit import limiter

router = APIRouter()

#: Bounds memory for the cache below; old entries (superseded data
#: versions, or just unpopular queries) age out under this cap.
_ROUTE_CACHE_MAXSIZE = 256

#: CLAUDE.md's `/route` cache key is `(start_terminal_id, ship_id, num_hops,
#: starting_budget, rank, cache_data_version)` (Phase 3, Task 21: `rank` --
#: the resolved 1-10 DP rank, not the raw 0-10 `risk_level` the client sent
#: -- added as a new component; see `_rank_from_risk_level` below). Without
#: it, two requests differing only in `risk_level` would collide on the same
#: cache entry and silently return whichever rank happened to be computed
#: first -- a real correctness bug, not just a missed optimization, since
#: `risk_level` changes which route is the *correct* answer. Implemented as
#: a small hand-rolled LRU (an `OrderedDict` + lock) rather than
#: `functools.lru_cache`, because `find_best_route()` needs the *actual*
#: `buy_prices`/`sell_prices` dicts to run -- and plain `dict`s are
#: unhashable, so they can never be part of an `lru_cache`-decorated
#: function's argument list (it hashes every argument to build the cache
#: key). `graph` rides along as an extra key component -- hashed/compared by
#: object identity, like any plain Python object (`networkx.Graph` defines
#: no custom `__eq__`/`__hash__`) -- for the same reason Phase 1's version of
#: this cache did: it pins the lookup to the *exact* graph/price-index
#: snapshot the caller already validated `start_terminal_id` against, rather
#: than re-fetching `get_graph_cache().get_snapshot()` in here and risking it
#: having raced ahead to a newer `rebuild()` in between (which would compute
#: against the new snapshot while getting cached under the old
#: `data_version`'s key). Since a new graph object is created by every
#: `rebuild()` (atomic swap, never in-place mutation -- `backend/graph/
#: cache.py`), its identity alone already changes exactly when
#: `data_version` does; `data_version` is kept as an explicit key component
#: anyway to match CLAUDE.md's literal cache key and for readability.
#: `ship_jump_range_gm`/`ship_cargo_capacity_scu` are *not* part of the key:
#: they're a deterministic function of `(ship_id, data_version)` (the same
#: ship's stats can't change without a refresh, which changes `data_version`
#: too), so including them would add no new cache-key entropy -- they're
#: only needed inside as plain computation inputs, exactly like
#: `buy_prices`/`sell_prices`.
_route_cache_lock = threading.Lock()
_route_cache: "OrderedDict[tuple, RouteSearchResult]" = OrderedDict()


def _rank_from_risk_level(risk_level: int) -> int:
    """Map a user-facing `risk_level` (`RouteRequest`, 0-10) to the internal
    1-indexed `rank` `find_best_route()` expects -- CLAUDE.md's Phase 3
    risk/reward mapping, exactly `rank = max(1, 10 - risk_level)`.
    `risk_level=10` (max risk tolerance, the `RouteRequest` default) ->
    `rank=1`, today's single best route, unchanged. `risk_level=0`
    (risk-averse) -> `rank=10`, the 10th-best distinct profitable route
    found (clamped by `find_best_route` itself if fewer than 10 distinct
    profitable routes exist).
    """
    return max(1, 10 - risk_level)


def _cached_search(
    graph: nx.DiGraph,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    start_terminal_id: int,
    ship_id: int,
    num_hops: int,
    starting_budget: float,
    rank: int,
    ship_jump_range_gm: float,
    ship_cargo_capacity_scu: float,
    data_version: int | None,
) -> RouteSearchResult:
    """Cache-or-compute `find_best_route()`. See the module-level comment
    above `_route_cache` for why this is a hand-rolled LRU rather than
    `functools.lru_cache`.
    """
    key = (id(graph), start_terminal_id, ship_id, num_hops, starting_budget, rank, data_version)

    with _route_cache_lock:
        cached = _route_cache.get(key)
        if cached is not None:
            _route_cache.move_to_end(key)
            return cached

    result = find_best_route(
        graph,
        start_terminal_id=start_terminal_id,
        num_hops=num_hops,
        starting_budget=starting_budget,
        ship_jump_range_gm=ship_jump_range_gm,
        ship_cargo_capacity_scu=ship_cargo_capacity_scu,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        settings=get_settings(),
        rank=rank,
    )

    with _route_cache_lock:
        _route_cache[key] = result
        _route_cache.move_to_end(key)
        while len(_route_cache) > _ROUTE_CACHE_MAXSIZE:
            _route_cache.popitem(last=False)

    return result


def _cached_search_cache_clear() -> None:
    """Test-only reset hook, mirroring `functools.lru_cache`'s
    `.cache_clear()` API so existing test isolation fixtures keep working
    unchanged against this hand-rolled cache.
    """
    with _route_cache_lock:
        _route_cache.clear()


_cached_search.cache_clear = _cached_search_cache_clear  # type: ignore[attr-defined]


def _to_route_response(
    graph: nx.DiGraph, commodity_names: dict[int, str], result: RouteSearchResult
) -> RouteResponse:
    if not result.found:
        return RouteResponse(
            found=False,
            start_terminal_id=result.start_terminal_id,
            starting_budget=result.starting_budget,
            final_cash=result.final_cash,
            total_distance=0.0,
            total_profit=0.0,
            message=result.message,
            requested_rank=result.requested_rank,
            actual_rank_used=result.actual_rank_used,
        )

    hops = [
        RouteHop(
            terminal_id=hop.terminal_id,
            terminal_name=graph.nodes[hop.terminal_id]["name"],
            commodity_id=hop.commodity_id,
            commodity_name=(
                commodity_names.get(hop.commodity_id) if hop.commodity_id is not None else None
            ),
            distance_from_previous=hop.distance_from_previous,
            quantity_traded=hop.quantity_traded,
            unit_buy_price=hop.unit_buy_price,
            unit_sell_price=hop.unit_sell_price,
            profit_this_hop=hop.profit_this_hop,
        )
        for hop in result.hops
    ]
    return RouteResponse(
        found=True,
        start_terminal_id=result.start_terminal_id,
        hops=hops,
        total_distance=result.total_distance,
        total_profit=result.total_profit,
        starting_budget=result.starting_budget,
        final_cash=result.final_cash,
        requested_rank=result.requested_rank,
        actual_rank_used=result.actual_rank_used,
    )


@router.post("/route", response_model=RouteResponse)
@limiter.limit(get_settings().rate_limit_route)
def search_route(payload: RouteRequest, request: Request) -> RouteResponse:
    settings = get_settings()
    snapshot = get_graph_cache().get_snapshot()
    graph = snapshot.graph

    # CLAUDE.md security ground rules: start_terminal_id and ship_id must be
    # checked against real known terminals/ships (404, not a raw lookup
    # failure); Pydantic (`RouteRequest`) already enforces `num_hops > 0` /
    # `starting_budget >= 0` -- the server-side *maximum* caps below are
    # runtime config, so they're enforced here rather than as static schema
    # constraints.
    if payload.start_terminal_id not in graph:
        raise HTTPException(status_code=404, detail="Unknown start_terminal_id.")

    with session_scope() as session:
        ship = session.get(Ship, payload.ship_id)
    if ship is None:
        raise HTTPException(status_code=404, detail="Unknown ship_id.")

    if payload.num_hops > settings.max_hops_cap:
        raise HTTPException(
            status_code=422,
            detail=f"num_hops exceeds the server-enforced maximum of {settings.max_hops_cap}.",
        )

    if payload.starting_budget > settings.max_starting_budget_cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"starting_budget exceeds the server-enforced maximum of "
                f"{settings.max_starting_budget_cap}."
            ),
        )

    rank = _rank_from_risk_level(payload.risk_level)

    result = _cached_search(
        graph,
        snapshot.buy_prices,
        snapshot.sell_prices,
        payload.start_terminal_id,
        payload.ship_id,
        payload.num_hops,
        payload.starting_budget,
        rank,
        ship.quantum_range_gm,
        ship.cargo_capacity_scu,
        snapshot.data_version,
    )
    return _to_route_response(graph, snapshot.commodity_names, result)
