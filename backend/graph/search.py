"""Label-setting, resource-constrained max-weight walk search.

Implements CLAUDE.md's "Route search problem" exactly: **not** vanilla
Dijkstra (a flipped-comparator Dijkstra would loop forever exploiting a
positive-weight cycle -- this graph has plenty, e.g. two profitable
same-orbit terminals trading opposite directions). The actual problem:
starting from a fixed node, find the walk (revisits/cycles allowed) that
maximizes total accumulated edge weight, subject to total distance
travelled <= a caller-chosen budget.

Algorithm (per CLAUDE.md, verbatim structure)
------------------------------------------------
- A *label* is `(current_node, distance_used_so_far, cumulative_weight,
  path)` -- `_Label` below.
- Explored with a max-priority queue ordered by `cumulative_weight`
  (best-first): a `heapq` min-heap keyed on `-cumulative_weight`, with a
  monotonic tiebreak counter so two labels with equal weight never need a
  `<` comparison between `_Label` objects themselves.
- From a label at `n`, for each outgoing edge `n->m` with
  `distance_used + d(n,m) <= max_distance` **and** `d(n,m) <=
  distance_threshold`, push a new label at `m`. (`distance_threshold` is a
  *per-hop* cap, separate from `max_distance`'s cumulative budget -- see
  `backend/graph/builder.py`'s module docstring for why this filter lives
  here and not in the graph itself.)
- **Dominance pruning:** at a given node, label A dominates label B if
  `A.distance_used <= B.distance_used and A.cumulative_weight >=
  B.cumulative_weight` -- a dominated label is never expanded further
  (though see "seen vs. expanded" below, it still counts as a real answer).
  Implemented as lazy deletion: `_ActiveLabels` tracks the current
  non-dominated set per node; a label popped off the heap that's no longer
  in its node's active set is silently skipped rather than expanded.
- **Anytime behavior / safety valves:** `settings.search_label_cap_per_node`
  bounds each node's active-label set (keep top-K by `cumulative_weight`,
  drop the rest); `settings.search_time_budget_seconds` bounds total
  wall-clock search time. Both stop the search from digging deeper, never
  from reporting an answer -- the best label already generated is always
  returned. This is why the search never fails or hangs, only ever returns
  "best found within budget."
- Termination is otherwise guaranteed unconditionally (not just by the
  above safety valves): every edge has strictly positive distance (`>=
  settings.min_distance_floor > 0`), so `distance_used` strictly increases
  every hop, bounding the number of hops any single path can take before
  exceeding `max_distance` -- even though the underlying graph has cycles.

"Seen" vs. "expanded": best-answer tracking
-----------------------------------------------
CLAUDE.md: "Best answer = highest-cumulative_weight label seen across all
nodes when the queue empties." A label is recorded as a `best_label`
candidate **at the moment it's generated** (pushed), not only if/when it's
later popped and expanded -- a dominated or cap-evicted label still
represents one real, complete, budget-respecting walk from the start node;
dominance/cap pruning only says "don't bother continuing search *from* this
state," never "this walk didn't happen." Discarding a dominated label's
candidacy for `best_label` would incorrectly throw away a valid answer.

"Found" vs. "no profitable route"
--------------------------------------
Every edge weight is `>= 0` (CLAUDE.md's formula floors at `max(0, ...)`),
so `cumulative_weight` is monotonically non-decreasing as a path grows and
the trivial zero-hop `start_label` (`cumulative_weight == 0.0`) is always a
lower bound on `best_label`. A search result is only reported as
`found=True` when `best_label.cumulative_weight > 0` -- a zero-weight
result (whether that's literally the zero-hop start label, or a walk made
entirely of zero-weight "bridge" edges that never reached anything
profitable) is reported as `found=False` with an explanatory message,
matching `RouteResponse`'s contract (`backend/models/schemas.py`) that
`found=False` is a normal, successful "nothing profitable here" outcome,
not an error. This one rule also transparently covers CLAUDE.md's
"isolated start node" case (zero viable outgoing edges) with no special
early-return needed: the main loop pops `start_label`, finds no expandable
successors, the heap empties, and `best_label` is still `start_label` --
`found=False` falls out naturally.

Validation is the caller's job
-----------------------------------
Per CLAUDE.md's security ground rules, checking that `start_terminal_id`
is a real, known terminal (404 if not) and that `max_distance` /
`distance_threshold` are positive and capped at `settings.
max_distance_cap` / `settings.distance_threshold_max` is the `/route`
router's responsibility, not this module's -- `find_best_route` is a pure
algorithm over whatever graph/parameters it's given. It still never
crashes on an out-of-graph `start_terminal_id` (returns a clean
`found=False` instead) purely as defense in depth for direct/test callers,
not as a substitute for the router's validation.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import time
from dataclasses import dataclass, field

import networkx as nx

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)

_NO_ROUTE_MESSAGE = "No profitable route found from this starting terminal under the given constraints."


@dataclass(eq=False)
class _Label:
    """One partial (or complete) walk from the start node.

    `eq=False` keeps identity-based equality/hashing (the default from
    `object`) instead of dataclass's auto-generated field-wise `__eq__` --
    `_ActiveLabels` and the heap's lazy-deletion check need to ask "is this
    the *same* label instance still active," not "does some other label
    happen to have equal field values."
    """

    node: int
    distance_used: float
    cumulative_weight: float
    path: tuple[int, ...]


class _ActiveLabels:
    """Per-node set of currently non-dominated labels, capped at top-K.

    Owns the dominance-pruning and label-cap bookkeeping described in the
    module docstring. A label evicted here (dominated, or capped out) is
    never explicitly removed from the heap -- `find_best_route`'s main loop
    does lazy deletion instead, skipping any popped label no longer present
    in its node's active set.
    """

    def __init__(self, label_cap: int) -> None:
        self._label_cap = label_cap
        self._by_node: dict[int, list[_Label]] = {}

    def is_active(self, label: _Label) -> bool:
        return label in self._by_node.get(label.node, ())

    def try_add(self, label: _Label) -> bool:
        """Attempt to admit `label` into its node's active set.

        Returns `True` if admitted (the caller should push it onto the
        search heap), `False` if it was dominated by an existing label (or,
        after admission, immediately evicted by the label cap).
        """
        existing = self._by_node.get(label.node, [])

        survivors: list[_Label] = []
        for other in existing:
            if other.distance_used <= label.distance_used and other.cumulative_weight >= label.cumulative_weight:
                # `other` already dominates the candidate -- nothing changes.
                return False
            if not (
                label.distance_used <= other.distance_used
                and label.cumulative_weight >= other.cumulative_weight
            ):
                survivors.append(other)
            # else: `label` dominates `other` -- `other` is dropped.

        survivors.append(label)
        if len(survivors) > self._label_cap:
            survivors.sort(key=lambda l: l.cumulative_weight, reverse=True)
            survivors = survivors[: self._label_cap]
            if label not in survivors:
                # `label` itself was the one capped out -- restore the
                # node's set to the (unchanged-by-`label`) survivors and
                # report non-admission.
                self._by_node[label.node] = survivors
                return False

        self._by_node[label.node] = survivors
        return True


@dataclass(frozen=True)
class RouteHopResult:
    """One leg of a found route -- a low-level, schema-independent shape.

    A future `/route` router maps this onto `RouteHop`
    (`backend/models/schemas.py`), filling in display names from the graph's
    node attributes / a commodity lookup that this module deliberately has
    no knowledge of.
    """

    terminal_id: int
    distance_from_previous: float
    profit_this_hop: float
    commodity_id: int | None


@dataclass(frozen=True)
class RouteSearchResult:
    """Outcome of `find_best_route()`.

    `found=False` <=> `hops == ()` <=> `total_distance == total_profit ==
    0.0`, kept consistent by construction here for the same reason
    `RouteResponse` enforces it at the schema layer.
    """

    found: bool
    start_terminal_id: int
    hops: tuple[RouteHopResult, ...] = ()
    total_distance: float = 0.0
    total_profit: float = 0.0
    total_weight: float = 0.0
    message: str | None = None


def _hops_from_path(graph: nx.DiGraph, path: tuple[int, ...]) -> tuple[RouteHopResult, ...]:
    hops = []
    for origin, destination in zip(path, path[1:]):
        edge = graph.edges[origin, destination]
        hops.append(
            RouteHopResult(
                terminal_id=destination,
                distance_from_previous=edge["distance"],
                profit_this_hop=edge["profit"],
                commodity_id=edge["best_commodity_id"],
            )
        )
    return tuple(hops)


def _not_found(start_terminal_id: int) -> RouteSearchResult:
    return RouteSearchResult(
        found=False, start_terminal_id=start_terminal_id, message=_NO_ROUTE_MESSAGE
    )


def find_best_route(
    graph: nx.DiGraph,
    start_terminal_id: int,
    max_distance: float,
    distance_threshold: float,
    settings: Settings | None = None,
) -> RouteSearchResult:
    """Best-first label-setting search for the max-weight walk from
    `start_terminal_id`, subject to `max_distance` (cumulative budget) and
    `distance_threshold` (per-hop cap). See module docstring for the full
    algorithm description, and its "Validation is the caller's job"
    section for what this function does and doesn't check on your behalf.
    """
    settings = settings or get_settings()

    if start_terminal_id not in graph or max_distance <= 0 or distance_threshold <= 0:
        return _not_found(start_terminal_id)

    start_label = _Label(
        node=start_terminal_id, distance_used=0.0, cumulative_weight=0.0, path=(start_terminal_id,)
    )
    best_label = start_label

    active = _ActiveLabels(label_cap=max(1, settings.search_label_cap_per_node))
    active.try_add(start_label)

    counter = itertools.count()
    heap: list[tuple[float, int, _Label]] = [(-start_label.cumulative_weight, next(counter), start_label)]

    deadline = time.monotonic() + settings.search_time_budget_seconds
    expansions = 0

    while heap:
        if time.monotonic() >= deadline:
            logger.info(
                "route search: time budget exhausted after %d expansions, returning best found",
                expansions,
            )
            break

        _, _, label = heapq.heappop(heap)
        if not active.is_active(label):
            continue  # lazily-deleted: dominated or capped out since it was pushed
        expansions += 1

        for successor in graph.successors(label.node):
            edge = graph.edges[label.node, successor]
            edge_distance = edge["distance"]
            if edge_distance > distance_threshold:
                continue
            new_distance_used = label.distance_used + edge_distance
            if new_distance_used > max_distance:
                continue

            new_label = _Label(
                node=successor,
                distance_used=new_distance_used,
                cumulative_weight=label.cumulative_weight + edge["weight"],
                path=label.path + (successor,),
            )

            # Recorded as a candidate answer regardless of whether dominance
            # pruning accepts it below -- see module docstring's "seen vs.
            # expanded" section.
            if new_label.cumulative_weight > best_label.cumulative_weight:
                best_label = new_label

            if active.try_add(new_label):
                heapq.heappush(heap, (-new_label.cumulative_weight, next(counter), new_label))

    if best_label.cumulative_weight <= 0:
        return _not_found(start_terminal_id)

    hops = _hops_from_path(graph, best_label.path)
    return RouteSearchResult(
        found=True,
        start_terminal_id=start_terminal_id,
        hops=hops,
        total_distance=sum(hop.distance_from_previous for hop in hops),
        total_profit=sum(hop.profit_this_hop for hop in hops),
        total_weight=best_label.cumulative_weight,
    )
