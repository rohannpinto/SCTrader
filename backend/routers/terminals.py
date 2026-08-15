"""`GET /terminals` -- lists every commodity-trading terminal known to the
current graph cache (e.g. for a frontend "start station" selector).

Served entirely from `GraphCache`, never a direct DB query, so the list a
client sees is always in sync with exactly the terminal set `/route`
searches over -- a terminal DB row that hasn't made it into the current
graph (e.g. between a refresh finishing and its `rebuild()` swap, though
those happen back-to-back) is not listed as a valid `start_terminal_id` yet
either.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.config import get_settings
from backend.graph.cache import get_graph_cache
from backend.models.schemas import TerminalOut
from backend.rate_limit import limiter

router = APIRouter()


@router.get("/terminals", response_model=list[TerminalOut])
@limiter.limit(get_settings().rate_limit_default)
def list_terminals(request: Request) -> list[TerminalOut]:
    graph = get_graph_cache().get_graph()
    terminals = [
        TerminalOut(
            id=terminal_id,
            name=attrs["name"],
            star_system_name=attrs.get("star_system_name"),
            location_name=attrs.get("location_name"),
        )
        for terminal_id, attrs in graph.nodes(data=True)
    ]
    terminals.sort(key=lambda terminal: terminal.name)
    return terminals
