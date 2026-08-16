"""Cache DB -> `networkx.DiGraph` builder.

`build_graph()` is the single public entrypoint. It is a **pure function**
of the cache DB's current contents: given an `Engine`, it bulk-loads every
commodity-trading `Terminal`, `Price`, and `Distance` row (three queries,
no per-row/per-terminal loops -- see CLAUDE.md's "no N+1 queries" rule) and
returns a freshly-built `networkx.DiGraph` plus the bulk price indices. It
never mutates any module-level state itself -- `backend/graph/cache.py`'s
`GraphCache` is what owns the "build once per refresh, atomic swap"
lifecycle described in CLAUDE.md; this module only knows how to build one
graph (and its accompanying price indices) from whatever is on disk right
now.

Phase 2 simplification (CLAUDE.md's "Data model & route search" section,
rewritten in place for this phase): a route's profit-maximizing commodity
now depends on how much cash the traveler has *when they arrive* at an
edge's origin terminal, which is a search-time fact, not a graph-build-time
one -- so this module no longer computes a per-edge `weight`/`profit`/
`best_commodity_id`. It bulk-loads the same `buy_prices`/`sell_prices`
indices it always has (`terminal_id -> {commodity_id: price}`), but instead
of consuming them internally to pick one "best" commodity per edge, it
returns them as part of `GraphBuildResult` so `backend/graph/cache.py` can
carry them on `GraphCacheSnapshot` for the search layer
(`backend/graph/search.py`) to consult per-hop, cash-aware, at query time.

Node and edge shape
--------------------
- **Nodes** are commodity-trading terminals only (`Terminal.
  is_commodity_trading` true) -- CLAUDE.md: "Non-commodity terminals
  (ship dealers, refuel-only, etc.) are filtered out during ingestion,"
  reaffirmed here as a defensive filter in case a non-commodity terminal
  ever ends up with `Distance` rows. Keyed by the internal `Terminal.id`
  surrogate (never an external wiki/UEX id). Every qualifying terminal
  becomes a node even if it ends up with zero edges -- this is what lets a
  router later tell "unknown terminal" (404) apart from "known but
  isolated terminal" (CLAUDE.md's "isolated start node" case, handled by
  the search in `backend/graph/search.py`).
- **Edges** are added for *every* `Distance` row between two
  commodity-trading terminals, unconditionally -- this module does **not**
  filter by any distance threshold. A ship's quantum range (Phase 2's
  per-hop distance filter, replacing the old `distance_threshold` request
  parameter) is a per-request search parameter, not a graph-build-time one;
  a graph rebuilt once per refresh cannot pre-filter by a value that varies
  per request (per selected ship). `backend/graph/search.py` applies that
  filter at query time over this same unfiltered graph. Each edge carries
  exactly one attribute:
    - `distance` (`float`): the raw distance from the `Distance` row,
      exactly as stored -- never floored here. This is what a route
      response reports as "distance travelled," and it must reflect the
      real in-game distance.

  Edges deliberately carry **no** `weight`/`profit`/`best_commodity_id`
  attribute in Phase 2 -- see module docstring's opening paragraph for why
  those can no longer be precomputed once profit depends on the traveler's
  cash on hand.

Missing vs. zero prices, and `min_distance_floor`'s Phase 2 role
------------------------------------------------------------------
`_index_prices` builds two separate dicts (`buy_prices`, `sell_prices`)
that simply omit `None` entries, so "missing" and "priced at 0" can never
be confused downstream (`backend/graph/search.py` relies on this same
discipline when picking a hop's commodity). `settings.min_distance_floor`
is **not** read anywhere in this module anymore: Phase 1 applied it here as
a weight-formula divide-by-zero guard, but that formula no longer exists.
Its role moved entirely to ingestion time
(`backend/ingest/refresh.py`, which floors same-orbit terminal pairs'
*stored* `Distance.distance` at write time) -- it now exists purely to
give same-orbit terminal pairs a sensible nonzero distance value, not to
protect a division that no longer happens here.

Guardrail
----------
`settings.graph_edge_count_guardrail` is a soft cap: if the built graph
ends up with more edges than the guardrail, `GraphBuildResult.warnings`
gets one human-readable entry and a `logger.warning` is emitted, but the
graph is still returned complete and usable -- CLAUDE.md's anytime-search
philosophy ("return best found within budget, never fail or hang") extends
to graph building too. `RefreshStatusOut.warnings`
(`backend/models/schemas.py`) is where a `/refresh-status` route surfaces
this to a client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
from sqlalchemy import Engine, select

from backend.config import Settings, get_settings
from backend.models.db import Commodity, Distance, Price, Terminal, session_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphBuildResult:
    """Return value of `build_graph()`: the graph, the bulk price indices,
    and any non-fatal warnings.

    `commodity_names` (`Commodity.id -> Commodity.name`) and the price
    indices ride alongside the graph so `backend/graph/cache.py` can carry
    all of it on one `GraphCacheSnapshot` -- a `/route` search (a later
    task) then needs no separate DB round trip on its hot path just to
    resolve a hop's commodity name or price.

    `buy_prices`/`sell_prices` are both shaped `terminal_id ->
    {commodity_id: price}`. A commodity/terminal pair with no row (or a
    `None` price) at the source is simply absent from the dict -- never
    inserted as `0.0` -- so "missing" and "known to be zero" stay
    distinguishable exactly as CLAUDE.md's data model requires.
    """

    graph: nx.DiGraph
    commodity_names: dict[int, str] = field(default_factory=dict)
    buy_prices: dict[int, dict[int, float]] = field(default_factory=dict)
    sell_prices: dict[int, dict[int, float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _load_commodity_terminal_ids(session) -> dict[int, Terminal]:
    """Bulk-load every commodity-trading terminal, keyed by internal `id`."""
    rows = session.execute(
        select(Terminal).where(Terminal.is_commodity_trading.is_(True))
    ).scalars()
    return {row.id: row for row in rows}


def _load_commodity_names(session) -> dict[int, str]:
    """Bulk-load every `Commodity` row as an `id -> name` display lookup."""
    return {row.id: row.name for row in session.execute(select(Commodity)).scalars()}


def _index_prices(
    session, commodity_terminal_ids: set[int]
) -> tuple[dict[int, dict[int, float]], dict[int, dict[int, float]]]:
    """Bulk-load every `Price` row, split into "has a buy price" / "has a
    sell price" lookups: `terminal_id -> {commodity_id: price}`.

    A `None` price is simply never inserted into either dict -- there is no
    sentinel to accidentally mistake for a real `0.0` price downstream (see
    module docstring's "missing vs. zero" section).
    """
    buy_prices: dict[int, dict[int, float]] = {}
    sell_prices: dict[int, dict[int, float]] = {}
    for row in session.execute(select(Price)).scalars():
        if row.terminal_id not in commodity_terminal_ids:
            continue  # defensive: a non-commodity terminal should have no rows anyway
        if row.price_buy is not None:
            buy_prices.setdefault(row.terminal_id, {})[row.commodity_id] = row.price_buy
        if row.price_sell is not None:
            sell_prices.setdefault(row.terminal_id, {})[row.commodity_id] = row.price_sell
    return buy_prices, sell_prices


def build_graph(
    engine: Optional[Engine] = None, settings: Optional[Settings] = None
) -> GraphBuildResult:
    """Bulk-load the cache DB and build a fresh `networkx.DiGraph` plus the
    bulk price indices.

    Pure with respect to module state: takes an optional `Engine` (defaults
    to the process-wide one via `session_scope`) and returns a brand new
    `GraphBuildResult` every call. Callers that need "build once per
    refresh, atomic swap into a shared cache" behavior get that from
    `backend/graph/cache.py`'s `GraphCache`, not from this function.
    """
    settings = settings or get_settings()
    warnings: list[str] = []

    graph = nx.DiGraph()

    with session_scope(engine) as session:
        terminals_by_id = _load_commodity_terminal_ids(session)
        commodity_terminal_ids = set(terminals_by_id)

        for terminal_id, terminal in terminals_by_id.items():
            graph.add_node(
                terminal_id,
                name=terminal.name,
                code=terminal.code,
                star_system_name=terminal.star_system_name,
                location_name=terminal.location_name,
            )

        buy_prices, sell_prices = _index_prices(session, commodity_terminal_ids)
        commodity_names = _load_commodity_names(session)

        distance_rows = list(session.execute(select(Distance)).scalars())

    edges_added = 0
    for row in distance_rows:
        if row.terminal_a_id not in commodity_terminal_ids:
            continue  # defensive: see module docstring's "Nodes" section
        if row.terminal_b_id not in commodity_terminal_ids:
            continue

        graph.add_edge(row.terminal_a_id, row.terminal_b_id, distance=row.distance)
        edges_added += 1

    logger.info(
        "graph build: nodes=%d edges=%d", graph.number_of_nodes(), graph.number_of_edges()
    )

    if edges_added > settings.graph_edge_count_guardrail:
        message = (
            f"graph has {edges_added} edges, exceeding graph_edge_count_guardrail="
            f"{settings.graph_edge_count_guardrail}; route search may be slower than usual"
        )
        logger.warning(message)
        warnings.append(message)

    return GraphBuildResult(
        graph=graph,
        commodity_names=commodity_names,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        warnings=warnings,
    )
