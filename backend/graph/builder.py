"""Cache DB -> `networkx.DiGraph` builder, implementing CLAUDE.md's blended
edge-weight formula.

`build_graph()` is the single public entrypoint. It is a **pure function**
of the cache DB's current contents: given an `Engine`, it bulk-loads every
commodity-trading `Terminal`, `Price`, and `Distance` row (three queries,
no per-row/per-terminal loops -- see CLAUDE.md's "no N+1 queries" rule) and
returns a freshly-built `networkx.DiGraph`. It never mutates any
module-level state itself -- the (future) `GraphCache` singleton
(`backend/graph/cache.py`) is what owns the "build once per refresh, atomic
swap" lifecycle described in CLAUDE.md; this module only knows how to build
one graph from whatever is on disk right now.

Node and edge shape
--------------------
- **Nodes** are commodity-trading terminals only (`Terminal.
  is_commodity_trading` true) -- CLAUDE.md: "Non-commodity terminals
  (ship dealers, refuel-only, etc.) are filtered out during ingestion,"
  reaffirmed here as a defensive filter in case a non-commodity terminal
  ever ends up with `Distance` rows. Keyed by the internal `Terminal.id`
  surrogate (never an external wiki/UEX id), per CLAUDE.md's "every other
  part of the app refers to terminals solely by the internal `id`." Every
  qualifying terminal becomes a node even if it ends up with zero edges --
  this is what lets a router later tell "unknown terminal" (404) apart from
  "known but isolated terminal" (CLAUDE.md's "isolated start node" case,
  handled by the label-setting search in a later task).
- **Edges** are added for *every* `Distance` row between two
  commodity-trading terminals, unconditionally -- this module does **not**
  filter by any distance threshold. `RouteRequest.distance_threshold`
  (`backend/models/schemas.py`) is a per-request search parameter (part of
  the `/route` LRU cache key per CLAUDE.md), not a graph-build-time one; a
  graph rebuilt once per refresh cannot pre-filter by a value that varies
  per request. The label-setting search (a later task) applies
  `distance_threshold` as a per-hop filter, and `max_distance` as the
  cumulative budget, both at query time over this same unfiltered graph.
  Each edge carries:
    - `distance` (`float`): the raw distance from the `Distance` row,
      exactly as stored -- never floored. This is what a route response
      reports as "distance travelled," and it must reflect the real
      in-game distance, not an artificial floor applied only to keep the
      weight formula's division well-defined.
    - `weight` (`float`): `max(0, profit) / max(distance, min_distance_floor)`
      per CLAUDE.md's formula -- see `_best_commodity_for_edge`.
    - `profit` (`float`): the raw `sell_price - buy_price` of whichever
      commodity achieved `weight` (`0.0` when no commodity was profitable
      on this edge) -- reported separately from `weight` so a route
      response's `profit_this_hop` never has to reverse a
      floor-adjusted division back into a dollar amount.
    - `best_commodity_id` (`int | None`): internal `Commodity.id` of the
      commodity that achieved `weight` on this edge (CLAUDE.md's
      "`best_commodity`" edge metadata, named `_id` here since it stores a
      surrogate key, not a display name) -- `None` when no commodity was
      profitable, in which case the edge still exists with `weight=0.0`
      per CLAUDE.md ("occasionally useful as a bridge hop").

Weight formula: missing vs. zero, and the distance floor
-----------------------------------------------------------
A commodity only participates in an edge's `max(...)` if it has **both** a
non-`None` `price_buy` at the origin terminal **and** a non-`None`
`price_sell` at the destination terminal (`_index_prices` builds two
separate dicts that simply omit `None` entries, so "missing" and "priced at
0" can never be confused downstream). `min_distance_floor` is re-applied to
the weight denominator here (`max(distance, settings.min_distance_floor)`)
even though `backend/ingest/refresh.py` already floors same-orbit terminal
pairs' *stored* distance at ingestion time -- CLAUDE.md frames the floor as
part of the weight formula itself ("never dividing by a raw distance that
could be zero"), so this is a deliberate defense-in-depth second
application, not redundant: it protects the formula even if a future
ingestion path ever stores an unfloored near-zero distance.

Guardrail
----------
`settings.graph_edge_count_guardrail` is a soft cap: if the built graph
ends up with more edges than the guardrail, `GraphBuildResult.warnings`
gets one human-readable entry and a `logger.warning` is emitted, but the
graph is still returned complete and usable -- CLAUDE.md's anytime-search
philosophy ("return best found within budget, never fail or hang") extends
to graph building too. `RefreshStatusOut.warnings`
(`backend/models/schemas.py`) is where a future `/refresh-status` route
would surface this to a client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
from sqlalchemy import Engine, select

from backend.config import Settings, get_settings
from backend.models.db import Distance, Price, Terminal, session_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphBuildResult:
    """Return value of `build_graph()`: the graph plus any non-fatal warnings."""

    graph: nx.DiGraph
    warnings: list[str] = field(default_factory=list)


def _load_commodity_terminal_ids(session) -> dict[int, Terminal]:
    """Bulk-load every commodity-trading terminal, keyed by internal `id`."""
    rows = session.execute(
        select(Terminal).where(Terminal.is_commodity_trading.is_(True))
    ).scalars()
    return {row.id: row for row in rows}


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


def _best_commodity_for_edge(
    origin_id: int,
    destination_id: int,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    floored_distance: float,
) -> tuple[float, float, Optional[int]]:
    """Implements CLAUDE.md's `max over all commodities c of ...` for one edge.

    Returns `(weight, profit, best_commodity_id)`. A commodity is a
    candidate only if it has a buy price at `origin_id` *and* a sell price
    at `destination_id` (the dict-membership check below is exactly that --
    `_index_prices` never stores a `None`). Candidates are walked in sorted
    `commodity_id` order so a tie between two equally-profitable commodities
    resolves deterministically (first by id) rather than by dict iteration
    order.

    Starts from `weight=0.0, profit=0.0, best_commodity_id=None` and only
    overwrites on a strictly-better weight, so "no commodity is profitable
    on this edge" naturally falls out as the CLAUDE.md-mandated
    `weight = 0` case, with no special-casing needed.
    """
    best_weight = 0.0
    best_profit = 0.0
    best_commodity_id: Optional[int] = None

    origin_buys = buy_prices.get(origin_id)
    destination_sells = sell_prices.get(destination_id)
    if not origin_buys or not destination_sells:
        return best_weight, best_profit, best_commodity_id

    candidate_commodity_ids = sorted(set(origin_buys) & set(destination_sells))
    for commodity_id in candidate_commodity_ids:
        profit = destination_sells[commodity_id] - origin_buys[commodity_id]
        if profit <= 0:
            continue  # max(0, profit) would be 0 -- can never beat best_weight > 0
        weight = profit / floored_distance
        if weight > best_weight:
            best_weight = weight
            best_profit = profit
            best_commodity_id = commodity_id

    return best_weight, best_profit, best_commodity_id


def build_graph(
    engine: Optional[Engine] = None, settings: Optional[Settings] = None
) -> GraphBuildResult:
    """Bulk-load the cache DB and build a fresh `networkx.DiGraph`.

    Pure with respect to module state: takes an optional `Engine` (defaults
    to the process-wide one via `session_scope`) and returns a brand new
    graph object every call. Callers that need "build once per refresh,
    atomic swap into a shared cache" behavior get that from
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

        distance_rows = list(session.execute(select(Distance)).scalars())

    floor = settings.min_distance_floor
    edges_added = 0
    for row in distance_rows:
        if row.terminal_a_id not in commodity_terminal_ids:
            continue  # defensive: see module docstring's "Nodes" section
        if row.terminal_b_id not in commodity_terminal_ids:
            continue

        floored_distance = max(row.distance, floor)
        weight, profit, best_commodity_id = _best_commodity_for_edge(
            row.terminal_a_id, row.terminal_b_id, buy_prices, sell_prices, floored_distance
        )
        graph.add_edge(
            row.terminal_a_id,
            row.terminal_b_id,
            distance=row.distance,
            weight=weight,
            profit=profit,
            best_commodity_id=best_commodity_id,
        )
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

    return GraphBuildResult(graph=graph, warnings=warnings)
