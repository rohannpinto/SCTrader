"""Bounded, hop-count-limited, cash/cargo-aware max-profit walk search.

Phase 2 replaces Phase 1's continuous-distance-budget label-setting search
(Pareto-frontier dominance pruning over `(distance_used, cumulative_weight)`)
with a fundamentally simpler algorithm, because the resource being spent is
now a small integer (a hop count) instead of a continuous quantity, and
per-hop profit now depends on real cash on hand rather than being a
precomputed per-edge constant. See CLAUDE.md's "Route search problem" for
the full narrative; this docstring covers the same ground at implementation
detail.

The problem
-----------
Starting from a fixed node with `starting_budget` cash and a ship of
`ship_cargo_capacity_scu` cargo capacity, find the walk of **at most**
`num_hops` hops (revisits/cycles allowed) that ends with the most cash on
hand, where each hop must cross an edge whose `distance <=
ship_jump_range_gm` (the ship's quantum range -- a hard per-hop filter, not
a weight denominator; this fully replaces Phase 1's `distance_threshold`
request parameter). At each hop, the traveler picks whichever tradeable
commodity (if any) maximizes that hop's *absolute* profit given the cash
they're carrying when they arrive at the hop's origin -- see
`_best_trade_for_edge`.

Why this needs no Pareto frontier (the key simplification)
------------------------------------------------------------
Phase 1's dominance pruning had to compare labels on *two* axes
(`distance_used`, `cumulative_weight`) because neither one dominated the
other -- a label with less distance used but less weight, and one with
more distance used but more weight, were genuinely incomparable; both had
to be kept as candidates.

Phase 2's state is `(node, hops_used, cash)`. Fix `(node, hops_used)`: does
a label with *more* cash ever end up worse off later than one with *less*
cash at the exact same node and hop count? No. At every future hop, the
quantity a label can afford is `min(floor(cash / buy_price),
ship_cargo_capacity_scu)` -- monotonically non-decreasing in `cash`, capped
by the *same* fixed `ship_cargo_capacity_scu` regardless of which label
we're extending. So a higher-cash label can always replicate every future
buying decision a lower-cash label at the same `(node, hops_used)` would
make (same commodities, same-or-larger quantities), and therefore ends up
with at least as much cash at every subsequent hop too. There is no second
axis to trade off against cash the way `distance_used` traded off against
`cumulative_weight` in Phase 1 -- more cash at a fixed `(node, hops_used)`
**weakly dominates** less cash, full stop.

This collapses the search from a Pareto-frontier label-setting search into
a plain bounded dynamic program: for each `(node, hops_used)` pair, only
the single highest-cash label reaching it is ever worth keeping, because
every other label at that same pair is provably no better in any future it
could reach. Implemented below as `hop_labels: list[dict[int, _Label]]`,
where `hop_labels[h]` is `{node: best_label_at_that_node_and_hop}` -- the
list-of-dicts is the same structure as a flat `dict[(node, hop)] ->
_Label]` keyed by tuple, just partitioned by hop for cheap per-level
iteration; `hop_labels[h][node]` and `dict[(node, h)]` carry identical
information. No label cap is needed (unlike Phase 1): the entire state
space is exactly `O(nodes * (num_hops + 1))`, already small and tightly
bounded by construction, not something that needs a heuristic top-K
safety valve to stay tractable.

Path reconstruction: parent pointers from the start
------------------------------------------------------
`_Label.previous` is a parent pointer, never a materialized path. This
project already hit a real, reviewed-and-fixed performance bug from
exactly the opposite mistake in the Phase 1 search (`git log --oneline`:
"search.py O(depth) path storage" -- rebuilding a label's path by
concatenating a new tuple at every hop made label creation itself
`O(depth)`, and left many long-lived labels each holding a largely
independent long tuple, dominating wall time in post-search
reference-counting teardown). Phase 2 is built with that lesson already
applied: every `_Label` is `O(1)` to create, and only the single winning
label, once, at the very end of `find_best_route`, ever pays to walk its
`previous` chain back to the start and materialize the actual route -- see
`_chain_from_label`.

Best answer: max cash across every `(node, hop)`, not just `hop ==
num_hops`
-------------------------------------------------------------------------
A dead-end node has no way to "pad" a great short route out to the full
`num_hops` with neutral hops, so requiring exactly `num_hops` would wrongly
disqualify it. The best answer is the maximum `cash` seen across every
`(node, hop)` pair with `hop` from `0` (the start label itself) to
`num_hops` inclusive -- same "anytime, best found within the resource
budget" philosophy Phase 1 used for its distance budget, just applied to
the hop dimension.

"Found" vs. "no profitable route"
-------------------------------------
`found=True` iff the best cash found is **strictly greater** than
`starting_budget` -- real, positive realized profit, not merely "didn't
lose money." This is a deliberate change from Phase 1's `cumulative_weight
> 0` check (weight could never be negative there; cash very much can stay
exactly flat across a walk of pure bridge hops, and must not be reported as
a "found" route). The isolated-start-node case (zero viable outgoing
edges) falls out of this with no special-casing: no hop-1 labels get
created at all, the only candidate is the `hop=0` starting label itself
(`cash == starting_budget`), which is not strictly greater than
`starting_budget`, so `found=False`.

Anytime behavior / safety valve
-----------------------------------
`settings.search_time_budget_seconds` bounds total wall-clock time, checked
once between fully-computed hop levels (never mid-level) so that if it ever
fires, `hop_labels` always holds only complete, internally-consistent hop
levels to compute the best answer from -- cheap defense in depth, not
expected to realistically fire given the tightly bounded `O(nodes *
num_hops)` state space this algorithm actually explores.

Validation is the caller's job
-----------------------------------
Per CLAUDE.md's security ground rules, checking `start_terminal_id` against
real known terminals (404 if not), `ship_id` against real known ships, and
`num_hops`/`starting_budget` bounds against server-configured maximums is
the `/route` router's responsibility, not this module's -- `find_best_route`
is a pure algorithm over whatever graph/parameters it's given. It still
never crashes on invalid input (out-of-graph start node, non-positive
`num_hops`/`ship_jump_range_gm`, negative `starting_budget`/
`ship_cargo_capacity_scu`) -- it returns a clean `found=False` instead,
purely as defense in depth for direct/test callers, not as a substitute for
the router's validation.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import networkx as nx

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)

_NO_ROUTE_MESSAGE = "No profitable route found from this starting terminal under the given constraints."


@dataclass(eq=False)
class _Label:
    """One walk of exactly `hops_used` hops from the start node, ending at
    `node` with `cash` on hand.

    `eq=False` keeps identity-based equality/hashing (the default from
    `object`) -- nothing here needs field-wise equality, only "is this the
    same label instance."

    `previous` is a parent pointer, not the full path -- see module
    docstring's "Path reconstruction" section. The remaining fields
    describe the single hop that produced this label from `previous` (all
    zero/`None` on the start label, which has no incoming hop):
    `distance_from_previous` the edge's raw distance, `commodity_id` the
    commodity traded (`None` for a neutral "bridge" hop),
    `quantity_traded`/`unit_buy_price`/`unit_sell_price` the trade's
    specifics, and `profit` the resulting `cash` delta (`0.0` for a bridge
    hop). Storing all of this on the label itself means path
    reconstruction never needs to re-consult the graph or price indices --
    the label chain alone is a complete record of the route.
    """

    node: int
    hops_used: int
    cash: float
    previous: "_Label | None"
    distance_from_previous: float = 0.0
    commodity_id: int | None = None
    quantity_traded: float = 0.0
    unit_buy_price: float | None = None
    unit_sell_price: float | None = None
    profit: float = 0.0


@dataclass(frozen=True)
class RouteHopResult:
    """One leg of a found route -- a low-level, schema-independent shape.

    A `/route` router maps this onto `RouteHop` (`backend/models/
    schemas.py`), filling in display names from the graph's node
    attributes / a commodity lookup that this module deliberately has no
    knowledge of.
    """

    terminal_id: int
    distance_from_previous: float
    commodity_id: int | None
    quantity_traded: float
    unit_buy_price: float | None
    unit_sell_price: float | None
    profit_this_hop: float


@dataclass(frozen=True)
class RouteSearchResult:
    """Outcome of `find_best_route()`.

    `found=False` <=> `hops == ()` <=> `final_cash == starting_budget` <=>
    `total_profit == 0.0`, kept consistent by construction here for the
    same reason `RouteResponse` enforces an analogous invariant at the
    schema layer.
    """

    found: bool
    start_terminal_id: int
    hops: tuple[RouteHopResult, ...] = ()
    total_distance: float = 0.0
    total_profit: float = 0.0
    starting_budget: float = 0.0
    final_cash: float = 0.0
    message: str | None = None


def _best_trade_for_edge(
    origin_id: int,
    destination_id: int,
    cash: float,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    ship_cargo_capacity_scu: float,
) -> tuple[int | None, float, float | None, float | None, float]:
    """Cash-aware per-hop commodity/quantity selection for edge
    `origin_id -> destination_id`.

    Returns `(commodity_id, quantity, unit_buy_price, unit_sell_price,
    profit)`. Same "set intersection of tradeable commodities" pattern as
    Phase 1's `builder.py::_best_commodity_for_edge`, just moved here and
    now cash-aware, since the profit-maximizing choice can no longer be
    precomputed at graph-build time (module docstring, and CLAUDE.md).

    For each commodity `c` with both a buy price at `origin_id` and a sell
    price at `destination_id` (dict-membership only -- `_index_prices`
    never stores a `None`, so "missing" and "priced at 0" can't be
    confused): `quantity = min(floor(cash / buy_price), cargo_capacity)`
    when `buy_price > 0`, else `quantity = cargo_capacity` (a free/
    zero-or-negative-cost commodity -- extremely unlikely given Task 13's
    curated commodity set, but handled rather than dividing by zero).
    `profit = quantity * (sell_price - buy_price)`, only a candidate when
    `sell_price > buy_price` *and* `quantity > 0`. The commodity maximizing
    **total** `profit` wins -- not per-unit margin -- since a cheaper
    commodity with a more affordable quantity can beat a pricier one with a
    better per-unit margin once cash is limited (see CLAUDE.md). Ties
    resolve by lowest `commodity_id` (candidates walked in sorted order,
    only a strictly-better profit overwrites the incumbent).

    If no commodity is profitable, this is a neutral "bridge" hop:
    `(None, 0.0, None, None, 0.0)`.
    """
    origin_buys = buy_prices.get(origin_id)
    destination_sells = sell_prices.get(destination_id)
    if not origin_buys or not destination_sells:
        return None, 0.0, None, None, 0.0

    best_commodity_id: int | None = None
    best_quantity = 0.0
    best_buy_price: float | None = None
    best_sell_price: float | None = None
    best_profit = 0.0

    for commodity_id in sorted(set(origin_buys) & set(destination_sells)):
        buy_price = origin_buys[commodity_id]
        sell_price = destination_sells[commodity_id]
        if sell_price <= buy_price:
            continue  # not profitable -- can never beat best_profit's initial 0.0

        if buy_price > 0:
            quantity = min(math.floor(cash / buy_price), ship_cargo_capacity_scu)
        else:
            quantity = ship_cargo_capacity_scu  # free/non-positive-cost commodity

        if quantity <= 0:
            continue

        profit = quantity * (sell_price - buy_price)
        if profit > best_profit:
            best_profit = profit
            best_commodity_id = commodity_id
            best_quantity = quantity
            best_buy_price = buy_price
            best_sell_price = sell_price

    return best_commodity_id, best_quantity, best_buy_price, best_sell_price, best_profit


def _chain_from_label(label: "_Label") -> list["_Label"]:
    """Materialize a label's full chain, start label first, by walking its
    `previous` pointers back and reversing.

    Only ever called once per search, on the single winning `best_label` --
    every other label generated during the search stays a cheap O(1)
    parent-pointer node and never pays this cost. See module docstring's
    "Path reconstruction" section.
    """
    chain: list[_Label] = []
    current: _Label | None = label
    while current is not None:
        chain.append(current)
        current = current.previous
    chain.reverse()
    return chain


def _hops_from_chain(chain: list["_Label"]) -> tuple[RouteHopResult, ...]:
    """Build the ordered hop results from a start-to-end label chain.

    Skips `chain[0]` (the start label, which has no incoming hop). Every
    field needed is already stored directly on each label -- no graph or
    price-index lookup required here at all.
    """
    hops = []
    for label in chain[1:]:
        hops.append(
            RouteHopResult(
                terminal_id=label.node,
                distance_from_previous=label.distance_from_previous,
                commodity_id=label.commodity_id,
                quantity_traded=label.quantity_traded,
                unit_buy_price=label.unit_buy_price,
                unit_sell_price=label.unit_sell_price,
                profit_this_hop=label.profit,
            )
        )
    return tuple(hops)


def _not_found(start_terminal_id: int, starting_budget: float) -> RouteSearchResult:
    return RouteSearchResult(
        found=False,
        start_terminal_id=start_terminal_id,
        starting_budget=starting_budget,
        final_cash=starting_budget,
        message=_NO_ROUTE_MESSAGE,
    )


def find_best_route(
    graph: nx.DiGraph,
    start_terminal_id: int,
    num_hops: int,
    starting_budget: float,
    ship_jump_range_gm: float,
    ship_cargo_capacity_scu: float,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    settings: Settings | None = None,
) -> RouteSearchResult:
    """Bounded DP search for the max-final-cash walk of at most `num_hops`
    hops from `start_terminal_id`, subject to `ship_jump_range_gm` (per-hop
    distance filter) and `ship_cargo_capacity_scu` (per-hop quantity cap).
    See module docstring for the full algorithm description and its
    "Validation is the caller's job" section for what this function does
    and doesn't check on your behalf.
    """
    settings = settings or get_settings()

    if (
        start_terminal_id not in graph
        or num_hops <= 0
        or starting_budget < 0
        or ship_jump_range_gm <= 0
        or ship_cargo_capacity_scu < 0
    ):
        return _not_found(start_terminal_id, starting_budget)

    start_label = _Label(
        node=start_terminal_id, hops_used=0, cash=starting_budget, previous=None
    )

    # `hop_labels[h]` is `{node: best_label_at_that_node_and_hop}` -- see
    # module docstring's "Why this needs no Pareto frontier" section for why
    # a single best-cash label per (node, hop) is provably sufficient.
    hop_labels: list[dict[int, _Label]] = [{start_terminal_id: start_label}]
    best_label = start_label

    deadline = time.monotonic() + settings.search_time_budget_seconds

    for hop in range(1, num_hops + 1):
        previous_frontier = hop_labels[hop - 1]
        if not previous_frontier:
            break  # no label survived to the previous hop -- nothing further to expand

        current_frontier: dict[int, _Label] = {}
        for node, label in previous_frontier.items():
            for successor in graph.successors(node):
                edge = graph.edges[node, successor]
                distance = edge["distance"]
                if distance > ship_jump_range_gm:
                    continue

                commodity_id, quantity, buy_price, sell_price, profit = _best_trade_for_edge(
                    node, successor, label.cash, buy_prices, sell_prices, ship_cargo_capacity_scu
                )
                candidate = _Label(
                    node=successor,
                    hops_used=hop,
                    cash=label.cash + profit,
                    previous=label,
                    distance_from_previous=distance,
                    commodity_id=commodity_id,
                    quantity_traded=quantity,
                    unit_buy_price=buy_price,
                    unit_sell_price=sell_price,
                    profit=profit,
                )

                existing = current_frontier.get(successor)
                if existing is None or candidate.cash > existing.cash:
                    current_frontier[successor] = candidate

        hop_labels.append(current_frontier)

        for label in current_frontier.values():
            if label.cash > best_label.cash:
                best_label = label

        if time.monotonic() >= deadline:
            logger.info(
                "route search: time budget exhausted after hop %d/%d, returning best found",
                hop,
                num_hops,
            )
            break

    if best_label.cash <= starting_budget:
        return _not_found(start_terminal_id, starting_budget)

    chain = _chain_from_label(best_label)
    hops = _hops_from_chain(chain)
    return RouteSearchResult(
        found=True,
        start_terminal_id=start_terminal_id,
        hops=hops,
        total_distance=sum(hop.distance_from_previous for hop in hops),
        total_profit=best_label.cash - starting_budget,
        starting_budget=starting_budget,
        final_cash=best_label.cash,
    )
