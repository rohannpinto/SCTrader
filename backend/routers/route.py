"""`POST /route` -- searches for the profit-maximizing walk from a starting
terminal, subject to a distance budget.

The actual algorithm lives entirely in `backend/graph/search.py`; this
router's job is request validation (CLAUDE.md's security ground rules),
resolving `distance_threshold`'s default/cap from settings, a small
self-invalidating LRU cache in front of the (not-cheap-on-a-dense-graph)
search, and translating the low-level `RouteSearchResult` into the API's
`RouteResponse` shape (filling in display names the search layer
deliberately has no knowledge of).
"""

from __future__ import annotations

import functools

import networkx as nx
from fastapi import APIRouter, HTTPException, Request

from backend.config import get_settings
from backend.graph.cache import get_graph_cache
from backend.graph.search import RouteSearchResult, find_best_route
from backend.models.schemas import RouteHop, RouteRequest, RouteResponse
from backend.rate_limit import limiter

router = APIRouter()

#: Bounds memory for the LRU cache below; old entries (superseded data
#: versions, or just unpopular queries) age out under this cap.
_ROUTE_CACHE_MAXSIZE = 256


@functools.lru_cache(maxsize=_ROUTE_CACHE_MAXSIZE)
def _cached_search(
    graph: nx.DiGraph,
    start_terminal_id: int,
    max_distance: float,
    distance_threshold: float,
    data_version: int | None,
) -> RouteSearchResult:
    """CLAUDE.md's `/route` LRU cache key is `(start_terminal_id,
    max_distance, distance_threshold, cache_data_version)`; `graph` rides
    along as an extra key component (hashed/compared by object identity,
    like any plain Python object -- `networkx.Graph` defines no custom
    `__eq__`/`__hash__`) purely so this function operates on the *exact*
    graph object the caller already validated `start_terminal_id` against,
    rather than re-fetching `get_graph_cache().get_graph()` in here and
    risking it having raced ahead to a newer `rebuild()` in between --
    which would compute against the new graph while getting cached under
    the old `data_version`'s key. Since a new graph object is created by
    every `rebuild()` (atomic swap, never in-place mutation --
    `backend/graph/cache.py`), its identity alone already changes exactly
    when `data_version` does; `data_version` is kept as an explicit
    parameter anyway to match CLAUDE.md's literal cache key and for
    readability.
    """
    return find_best_route(
        graph,
        start_terminal_id=start_terminal_id,
        max_distance=max_distance,
        distance_threshold=distance_threshold,
        settings=get_settings(),
    )


def _to_route_response(
    graph: nx.DiGraph, commodity_names: dict[int, str], result: RouteSearchResult
) -> RouteResponse:
    if not result.found:
        return RouteResponse(
            found=False, start_terminal_id=result.start_terminal_id, message=result.message
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
    )


@router.post("/route", response_model=RouteResponse)
@limiter.limit(get_settings().rate_limit_route)
def search_route(payload: RouteRequest, request: Request) -> RouteResponse:
    settings = get_settings()
    snapshot = get_graph_cache().get_snapshot()
    graph = snapshot.graph

    # CLAUDE.md security ground rules: start_terminal_id must be checked
    # against real known terminals (404, not a raw lookup failure); Pydantic
    # (`RouteRequest`) already enforces `max_distance`/`distance_threshold`
    # positivity -- the server-side *maximum* caps below are runtime config,
    # so they're enforced here rather than as static schema constraints.
    if payload.start_terminal_id not in graph:
        raise HTTPException(status_code=404, detail="Unknown start_terminal_id.")

    if payload.max_distance > settings.max_distance_cap:
        raise HTTPException(
            status_code=422,
            detail=f"max_distance exceeds the server-enforced maximum of {settings.max_distance_cap}.",
        )

    distance_threshold = (
        payload.distance_threshold
        if payload.distance_threshold is not None
        else settings.distance_threshold_default
    )
    if distance_threshold > settings.distance_threshold_max:
        raise HTTPException(
            status_code=422,
            detail=(
                f"distance_threshold exceeds the server-enforced maximum of "
                f"{settings.distance_threshold_max}."
            ),
        )

    result = _cached_search(
        graph, payload.start_terminal_id, payload.max_distance, distance_threshold, snapshot.data_version
    )
    return _to_route_response(graph, snapshot.commodity_names, result)
