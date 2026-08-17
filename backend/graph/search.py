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
`ship_cargo_capacity_scu`, non-positive `rank`) -- it returns a clean
`found=False` instead, purely as defense in depth for direct/test callers,
not as a substitute for the router's validation.

Phase 3: top-K route ranking (`rank`)
-----------------------------------------
Phase 3 adds a `rank: int = 1` parameter for risk/reward route ranking (see
the approved plan's "Phase 3" section): instead of only ever returning the
single best-cash route, a caller can ask for the `rank`-th best *distinct*
route by final cash. `rank=1` is, and must stay, byte-for-byte identical in
both behavior and performance to the original Phase 2 algorithm above --
`find_best_route` dispatches to `_find_single_best_route` (the untouched
original single-best-label-per-`(node, hop)` DP, zero added bookkeeping) for
`rank <= 1`, and to `_find_top_k_routes` (the generalized top-K DP below)
only when `rank > 1`.

**Why "keep the top-`rank` labels per `(node, hop)`, discard the rest" finds
the true global `rank`-th-best distinct route -- not a heuristic, a direct
generalization of this module's existing monotonicity proof, one level up:**
fix a `(node, hops_used)` state. As already proved above, more cash at that
state always weakly dominates less cash under *any* identical future
extension (same edges applied to a higher-cash label can never end up with
less cash than applying them to a lower-cash label, because `quantity` is
monotonically non-decreasing in `cash`, capped by the same fixed
`ship_cargo_capacity_scu` either way). Consequence: if a label `B` is not
among the top-`rank` labels retained at `(node, hops_used)`, there are
already `rank` *other*, distinct-by-construction labels at that exact same
`(node, hops_used)` with cash >= `B`'s. That's true in both of `B`'s
possible roles:

- **As a candidate final answer in its own right** (every label, at
  whatever hop it was created, is itself a complete, valid route of that
  many hops) -- `B` is directly beaten by `rank` other same-length routes
  ending at the same node, so it cannot be the global `rank`-th best.
- **As a prefix `B` might extend further**: for *any* future sequence of
  edges `B` might have taken from here, each of the `rank` retained labels
  ranked above `B`, extended via that identical sequence, ends up with
  cash >= what `B`'s extension would have produced (same monotonicity
  argument, applied to the suffix). So whatever final route `B` might have
  produced is dominated by `rank` other *actually reachable* routes built
  from a retained label plus that same suffix.

Applying this inductively hop-by-hop (a retained label surviving fewer than
`rank`-deep at some *later* state is, by the same argument, itself dominated
by `rank` other retained labels at that later state, and so on) means no
discarded label -- at any point along its walk -- can ever be needed to
compute the true global top-`rank` distinct routes. Each retained label
still carries its own unique parent-pointer chain (see "Path reconstruction"
above -- unchanged, no list/tuple path concatenation reintroduced), so
`rank` labels at a state are `rank` genuinely distinct routes by
construction, not just `rank` different cash numbers that might collide on
the same underlying path.

**Mechanics** (`_find_top_k_routes`): `hop_labels[h]` becomes
`{node: [best_label, ..., rank-th_best_label]}` -- a small
sorted-by-cash-descending list per `(node, hop)` instead of a single label.
The transition step is otherwise identical to the `rank=1` DP: every
retained label at `(node, hop - 1)` tries every ship-range-viable outgoing
edge exactly as `_best_trade_for_edge` already does, producing one candidate
per `(label, edge)` pair; candidates are grouped by destination node
(merging contributions from *every* predecessor feeding that node at this
hop, not just one edge's worth), then each destination's candidate pool is
truncated down to its own top-`rank` by cash. The final answer gathers every
retained label across every `(node, hop)` with `hop <= num_hops`, keeps only
the ones with `cash > starting_budget` (same "found" semantics as `rank=1`
-- a neutral/unprofitable label is not a "route" for ranking purposes
either), sorts that pool by cash descending, and returns the element at
1-indexed position `rank` -- or, if fewer than `rank` profitable labels
exist at all, clamps down to the least-profitable one actually found
(`RouteSearchResult.actual_rank_used` records which rank was actually used,
distinct from `requested_rank` only when this clamp fires). If *no*
profitable label exists at all, the result is `found=False` regardless of
what rank was requested -- identical "no profitable route" semantics to
`rank=1`, just evaluated against the whole pool instead of a single label.

State space for `rank > 1` is `O(nodes * num_hops * rank)` for retained
labels, with `O(nodes * num_hops * rank * average_out_degree)` candidate
labels considered along the way (each hop's every retained label tries
every outgoing edge, same as the `rank=1` DP just multiplied by up to
`rank` starting labels per node) -- still small and exactly bounded by
construction, no heuristic safety valve needed, though the existing
`settings.search_time_budget_seconds` anytime check still applies as
defense in depth (see `tests/perf/test_perf.py` for freshly-measured
`rank=10` timing).
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

    `requested_rank`/`actual_rank_used` (Phase 3's top-K route ranking --
    see module docstring's "Phase 3: top-K route ranking" section):
    `requested_rank` is simply the `rank` `find_best_route` was called with.
    `actual_rank_used` is the 1-indexed position, within the sorted-by-cash-
    descending pool of every genuinely profitable (`cash > starting_budget`)
    label found, of the route actually returned -- `1` for the `rank=1`
    path (and for a `rank > 1` request that found at least `rank` distinct
    profitable routes), clamped down to whatever *was* found when fewer
    than `requested_rank` distinct profitable routes exist (never
    `found=False` merely because the requested rank was too high, as long
    as at least one profitable route exists at all). `actual_rank_used == 0`
    whenever `found is False` -- no route was returned to attach a rank to.
    """

    found: bool
    start_terminal_id: int
    hops: tuple[RouteHopResult, ...] = ()
    total_distance: float = 0.0
    total_profit: float = 0.0
    starting_budget: float = 0.0
    final_cash: float = 0.0
    message: str | None = None
    requested_rank: int = 1
    actual_rank_used: int = 0


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
    zero-or-negative-cost commodity). As of Phase 2 Task 18, this branch is
    provably unreachable via real ingested data specifically: `backend/
    ingest/refresh.py`'s `_normalize_zero_price` now normalizes a literal
    `0` buy/sell price (the wiki API's real signal for "not traded in this
    direction here", empirically confirmed -- see that module's "Zero-price
    normalization" docstring section) to `None` at ingestion time, and a
    `None` price never makes it into `buy_prices`/`sell_prices` at all
    (`_index_prices`' set-intersection discipline above). This branch is
    kept anyway, deliberately, as defensive code: any caller that hands
    `find_best_route` a `buy_prices` dict with a literal `0.0` directly
    (e.g. a unit test, or a future non-ingestion caller) still gets
    well-defined, documented behavior rather than a `ZeroDivisionError`.
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
            # Defensive only -- unreachable via real ingested data since
            # Task 18's ingestion-time zero-price normalization; see this
            # function's docstring.
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


def _not_found(
    start_terminal_id: int, starting_budget: float, requested_rank: int = 1
) -> RouteSearchResult:
    return RouteSearchResult(
        found=False,
        start_terminal_id=start_terminal_id,
        starting_budget=starting_budget,
        final_cash=starting_budget,
        message=_NO_ROUTE_MESSAGE,
        requested_rank=requested_rank,
        actual_rank_used=0,
    )


def _top_k_by_cash(labels: list["_Label"], k: int) -> list["_Label"]:
    """Return (up to) the `k` highest-cash labels from `labels`, sorted
    descending by cash. Ties preserve `labels`' original relative order
    (`sorted` is stable) -- with `hop_labels` built up in deterministic
    iteration order (dict insertion order + `graph.successors()`'s
    deterministic order), this keeps `_find_top_k_routes` deterministic
    run to run for a fixed graph/inputs, same as `_find_single_best_route`
    already is.
    """
    return sorted(labels, key=lambda label: label.cash, reverse=True)[:k]


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
    rank: int = 1,
) -> RouteSearchResult:
    """Bounded DP search for the `rank`-th best (by final cash) distinct
    walk of at most `num_hops` hops from `start_terminal_id`, subject to
    `ship_jump_range_gm` (per-hop distance filter) and
    `ship_cargo_capacity_scu` (per-hop quantity cap). See module docstring
    for the full algorithm description -- both the original `rank=1`
    single-best DP and Phase 3's top-K generalization -- and its
    "Validation is the caller's job" section for what this function does
    and doesn't check on your behalf.

    `rank` defaults to `1`: the single best route, identical in behavior and
    performance to the pre-Phase-3 algorithm (dispatches straight to
    `_find_single_best_route`, which is that original implementation,
    untouched). `rank > 1` dispatches to `_find_top_k_routes`, the
    generalized top-K DP -- see module docstring's "Phase 3: top-K route
    ranking" section for the correctness argument.
    """
    settings = settings or get_settings()

    if (
        start_terminal_id not in graph
        or num_hops <= 0
        or starting_budget < 0
        or ship_jump_range_gm <= 0
        or ship_cargo_capacity_scu < 0
        or rank < 1
    ):
        return _not_found(start_terminal_id, starting_budget, requested_rank=rank)

    if rank == 1:
        return _find_single_best_route(
            graph,
            start_terminal_id,
            num_hops,
            starting_budget,
            ship_jump_range_gm,
            ship_cargo_capacity_scu,
            buy_prices,
            sell_prices,
            settings,
        )

    return _find_top_k_routes(
        graph,
        start_terminal_id,
        num_hops,
        starting_budget,
        ship_jump_range_gm,
        ship_cargo_capacity_scu,
        buy_prices,
        sell_prices,
        settings,
        rank,
    )


def _find_single_best_route(
    graph: nx.DiGraph,
    start_terminal_id: int,
    num_hops: int,
    starting_budget: float,
    ship_jump_range_gm: float,
    ship_cargo_capacity_scu: float,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    settings: Settings,
) -> RouteSearchResult:
    """The original Phase 2 algorithm, unmodified: a single best-cash label
    per `(node, hop)`, no top-K bookkeeping at all. Called only for
    `rank == 1` (all inputs already validated by `find_best_route`) -- this
    is what makes `rank=1` byte-for-byte identical in behavior and
    performance to before Phase 3 existed. See module docstring's "Why this
    needs no Pareto frontier" section.
    """
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
        return _not_found(start_terminal_id, starting_budget, requested_rank=1)

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
        requested_rank=1,
        actual_rank_used=1,
    )


def _find_top_k_routes(
    graph: nx.DiGraph,
    start_terminal_id: int,
    num_hops: int,
    starting_budget: float,
    ship_jump_range_gm: float,
    ship_cargo_capacity_scu: float,
    buy_prices: dict[int, dict[int, float]],
    sell_prices: dict[int, dict[int, float]],
    settings: Settings,
    rank: int,
) -> RouteSearchResult:
    """Phase 3's generalized top-K DP. Called only for `rank > 1` (all
    inputs already validated by `find_best_route`). See module docstring's
    "Phase 3: top-K route ranking" section for the full mechanics and
    correctness argument -- summary: identical transition step to
    `_find_single_best_route`, just applied to every one of a `(node, hop)`
    state's up-to-`rank` retained labels instead of only the single best,
    with every predecessor's contributions merged into each destination
    state before that state's own top-`rank` set is (re)selected.
    """
    start_label = _Label(
        node=start_terminal_id, hops_used=0, cash=starting_budget, previous=None
    )

    # `hop_labels[h]` is `{node: [best_label, ..., up to rank-th_best_label]}`,
    # sorted descending by cash -- the top-K generalization of
    # `_find_single_best_route`'s `{node: best_label}`.
    hop_labels: list[dict[int, list[_Label]]] = [{start_terminal_id: [start_label]}]

    deadline = time.monotonic() + settings.search_time_budget_seconds

    for hop in range(1, num_hops + 1):
        previous_frontier = hop_labels[hop - 1]
        if not previous_frontier:
            break  # no label survived to the previous hop -- nothing further to expand

        # Every retained label at every predecessor node contributes its
        # candidates here first; only once every predecessor's edges have
        # been tried is each destination node's pool truncated down to its
        # own top-`rank` -- this is what "merged into the destination
        # (node, hop)'s own top-rank set, competing with labels arriving
        # from OTHER predecessor nodes too" means in practice.
        raw_candidates: dict[int, list[_Label]] = {}
        for node, labels in previous_frontier.items():
            for label in labels:
                for successor in graph.successors(node):
                    edge = graph.edges[node, successor]
                    distance = edge["distance"]
                    if distance > ship_jump_range_gm:
                        continue

                    commodity_id, quantity, buy_price, sell_price, profit = _best_trade_for_edge(
                        node,
                        successor,
                        label.cash,
                        buy_prices,
                        sell_prices,
                        ship_cargo_capacity_scu,
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
                    raw_candidates.setdefault(successor, []).append(candidate)

        current_frontier: dict[int, list[_Label]] = {
            node: _top_k_by_cash(candidates, rank) for node, candidates in raw_candidates.items()
        }
        hop_labels.append(current_frontier)

        if time.monotonic() >= deadline:
            logger.info(
                "route search (rank=%d): time budget exhausted after hop %d/%d, returning best found",
                rank,
                hop,
                num_hops,
            )
            break

    # Gather every retained label across every (node, hop) actually
    # computed (hop=0's start label included, but it can never pass the
    # `cash > starting_budget` filter -- cash is unchanged there by
    # definition). Same "found" semantics as `_find_single_best_route`:
    # only strictly-profitable labels count as a "route" at all.
    profitable = [
        label
        for frontier in hop_labels
        for labels in frontier.values()
        for label in labels
        if label.cash > starting_budget
    ]

    if not profitable:
        return _not_found(start_terminal_id, starting_budget, requested_rank=rank)

    profitable.sort(key=lambda label: label.cash, reverse=True)
    actual_rank_used = min(rank, len(profitable))
    winning_label = profitable[actual_rank_used - 1]

    chain = _chain_from_label(winning_label)
    hops = _hops_from_chain(chain)
    return RouteSearchResult(
        found=True,
        start_terminal_id=start_terminal_id,
        hops=hops,
        total_distance=sum(hop.distance_from_previous for hop in hops),
        total_profit=winning_label.cash - starting_budget,
        starting_budget=starting_budget,
        final_cash=winning_label.cash,
        requested_rank=rank,
        actual_rank_used=actual_rank_used,
    )
