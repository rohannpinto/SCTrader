"""Unit tests for `backend/graph/search.py`.

Builds small `networkx.DiGraph` fixtures directly (not via
`backend/graph/builder.py`) so each test can hand-craft exactly the
distance/weight/profit/commodity edge attributes it needs, independent of
the weight-formula implementation covered by `test_graph_builder.py`.
"""

from __future__ import annotations

import networkx as nx
import pytest

from backend.config import Settings
from backend.graph.search import find_best_route


def _graph(edges: list[tuple[int, int, float, float, float, int | None]]) -> nx.DiGraph:
    """`edges` items are `(a, b, distance, weight, profit, best_commodity_id)`."""
    graph = nx.DiGraph()
    for a, b, distance, weight, profit, commodity_id in edges:
        graph.add_node(a)
        graph.add_node(b)
        graph.add_edge(
            a, b, distance=distance, weight=weight, profit=profit, best_commodity_id=commodity_id
        )
    return graph


def _settings(**overrides) -> Settings:
    defaults: dict = dict(search_label_cap_per_node=50, search_time_budget_seconds=2.0)
    defaults.update(overrides)
    return Settings(**defaults)


# --- basic found / not-found -------------------------------------------------


def test_simple_profitable_edge_is_found():
    graph = _graph([(1, 2, 10.0, 5.0, 50.0, 7)])

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=100.0, distance_threshold=100.0, settings=_settings()
    )

    assert result.found is True
    assert result.start_terminal_id == 1
    assert len(result.hops) == 1
    hop = result.hops[0]
    assert hop.terminal_id == 2
    assert hop.distance_from_previous == 10.0
    assert hop.profit_this_hop == 50.0
    assert hop.commodity_id == 7
    assert result.total_distance == 10.0
    assert result.total_profit == 50.0
    assert result.total_weight == 5.0


def test_isolated_start_node_returns_not_found_cleanly():
    graph = nx.DiGraph()
    graph.add_node(1)

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=100.0, distance_threshold=100.0, settings=_settings()
    )

    assert result.found is False
    assert result.hops == ()
    assert result.total_distance == 0.0
    assert result.total_profit == 0.0
    assert result.message


def test_unknown_start_terminal_returns_not_found_not_crash():
    graph = _graph([(1, 2, 10.0, 5.0, 50.0, 1)])

    result = find_best_route(
        graph, start_terminal_id=999, max_distance=100.0, distance_threshold=100.0, settings=_settings()
    )

    assert result.found is False
    assert result.hops == ()


def test_all_zero_weight_edges_reachable_reports_not_found():
    # Every edge exists (still costs budget) but nothing is profitable
    # anywhere reachable -- must not be reported as a "found" wandering route.
    graph = _graph(
        [
            (1, 2, 10.0, 0.0, 0.0, None),
            (2, 3, 10.0, 0.0, 0.0, None),
            (3, 1, 10.0, 0.0, 0.0, None),
        ]
    )

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=1000.0, distance_threshold=100.0, settings=_settings()
    )

    assert result.found is False


# --- max_distance budget ------------------------------------------------------


def test_edge_exceeding_max_distance_budget_is_not_taken():
    graph = _graph([(1, 2, 500.0, 5.0, 50.0, 1)])

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=100.0, distance_threshold=1000.0, settings=_settings()
    )

    assert result.found is False


def test_route_stays_within_cumulative_budget_across_hops():
    graph = _graph(
        [
            (1, 2, 40.0, 2.0, 20.0, 1),
            (2, 3, 40.0, 3.0, 30.0, 1),
            (3, 4, 40.0, 4.0, 40.0, 1),  # cumulative distance 120 > budget of 100
        ]
    )

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=100.0, distance_threshold=1000.0, settings=_settings()
    )

    assert result.found is True
    visited = [1] + [hop.terminal_id for hop in result.hops]
    assert visited == [1, 2, 3]  # third hop would blow the budget
    assert result.total_distance == 80.0


# --- distance_threshold (per-hop cap) -----------------------------------------


def test_edge_exceeding_distance_threshold_is_never_taken():
    graph = _graph([(1, 2, 50.0, 5.0, 50.0, 1)])

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=1000.0, distance_threshold=10.0, settings=_settings()
    )

    assert result.found is False


# --- best-first correctness: picks higher cumulative weight ------------------


def test_prefers_higher_cumulative_weight_over_fewer_hops():
    graph = _graph(
        [
            (1, 2, 10.0, 100.0, 1000.0, 1),  # direct, high weight
            (1, 3, 10.0, 1.0, 10.0, 2),  # low-weight detour...
            (3, 2, 10.0, 1.0, 10.0, 2),  # ...that sums to less than the direct edge
        ]
    )

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=1000.0, distance_threshold=1000.0, settings=_settings()
    )

    assert result.found is True
    assert [hop.terminal_id for hop in result.hops] == [2]
    assert result.total_weight == 100.0


def test_zero_weight_bridge_hop_leading_to_profit_is_taken():
    # First hop is an unprofitable bridge (weight 0); second hop is highly
    # profitable but only reachable through the bridge.
    graph = _graph(
        [
            (1, 2, 10.0, 0.0, 0.0, None),
            (2, 3, 10.0, 10.0, 100.0, 5),
        ]
    )

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=1000.0, distance_threshold=1000.0, settings=_settings()
    )

    assert result.found is True
    assert [hop.terminal_id for hop in result.hops] == [2, 3]
    assert result.total_profit == 100.0


# --- positive-weight cycles must terminate, not hang --------------------------


def test_positive_weight_cycle_terminates_within_budget():
    # A profitable 2-cycle: naive flipped-comparator Dijkstra would loop
    # forever exploiting it. Distance budget bounds the number of laps.
    graph = _graph(
        [
            (1, 2, 5.0, 1.0, 5.0, 1),
            (2, 1, 5.0, 1.0, 5.0, 1),
        ]
    )

    result = find_best_route(
        graph, start_terminal_id=1, max_distance=50.0, distance_threshold=100.0, settings=_settings()
    )

    assert result.found is True
    # 50 distance budget / 5 per hop = at most 10 hops.
    assert len(result.hops) <= 10
    assert result.total_distance <= 50.0


# --- invalid inputs handled gracefully -----------------------------------------


@pytest.mark.parametrize("max_distance,distance_threshold", [(0.0, 10.0), (-5.0, 10.0), (10.0, 0.0), (10.0, -1.0)])
def test_non_positive_budgets_return_not_found_not_crash(max_distance, distance_threshold):
    graph = _graph([(1, 2, 10.0, 5.0, 50.0, 1)])

    result = find_best_route(
        graph,
        start_terminal_id=1,
        max_distance=max_distance,
        distance_threshold=distance_threshold,
        settings=_settings(),
    )

    assert result.found is False


# --- time budget: still returns a valid answer, doesn't hang -----------------


def test_time_budget_exhaustion_still_returns_best_found_so_far():
    # A moderately-branching graph with a near-zero time budget: the search
    # must bail out quickly and still return whatever best label it found
    # (at minimum, nothing crashes and the result is well-formed).
    edges = []
    for i in range(1, 30):
        edges.append((i, i + 1, 5.0, float(i), float(i) * 5.0, i))
    graph = _graph(edges)

    result = find_best_route(
        graph,
        start_terminal_id=1,
        max_distance=1000.0,
        distance_threshold=1000.0,
        settings=_settings(search_time_budget_seconds=0.0),
    )

    # Well-formed regardless of outcome: found<=>non-empty hops, no crash.
    if result.found:
        assert len(result.hops) >= 1
        assert result.total_distance > 0
    else:
        assert result.hops == ()


# --- label cap: search still returns a valid, budget-respecting answer -------


def test_label_cap_still_returns_valid_answer_on_dense_graph():
    # Small complete-ish digraph with a tight label cap -- exercises
    # dominance/cap pruning without asserting a specific optimal path
    # (cap=1 can legitimately sacrifice global optimality; it must never
    # sacrifice correctness/validity).
    edges = []
    for a in range(1, 6):
        for b in range(1, 6):
            if a != b:
                edges.append((a, b, 10.0, 1.0, 10.0, 1))
    graph = _graph(edges)

    result = find_best_route(
        graph,
        start_terminal_id=1,
        max_distance=40.0,
        distance_threshold=100.0,
        settings=_settings(search_label_cap_per_node=1),
    )

    assert result.found is True
    assert result.total_distance <= 40.0
    # Every reported hop must correspond to a real edge in the graph.
    path = [1] + [hop.terminal_id for hop in result.hops]
    for origin, destination in zip(path, path[1:]):
        assert graph.has_edge(origin, destination)
