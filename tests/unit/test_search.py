"""Unit tests for `backend/graph/search.py` (Phase 2 hop-count/cash/cargo
search).

Builds small `networkx.DiGraph` fixtures directly (not via
`backend/graph/builder.py`) so each test can hand-craft exactly the
`distance` edge attribute and `buy_prices`/`sell_prices` indices it needs,
independent of `backend/graph/builder.py`'s bulk-loading (covered by
`test_graph_builder.py`). Every non-trivial numeric expectation below is
hand-computed in a comment so the test is checking arithmetic, not just
"something non-crashing came back."
"""

from __future__ import annotations

import random
import time

import networkx as nx
import pytest

from backend.config import Settings
from backend.graph.search import find_best_route

PricesByTerminal = dict[int, dict[int, float]]


def _graph(edges: list[tuple[int, int, float]]) -> nx.DiGraph:
    """`edges` items are `(a, b, distance)`."""
    graph = nx.DiGraph()
    for a, b, distance in edges:
        graph.add_node(a)
        graph.add_node(b)
        graph.add_edge(a, b, distance=distance)
    return graph


def _settings(**overrides) -> Settings:
    defaults: dict = dict(search_time_budget_seconds=2.0)
    defaults.update(overrides)
    return Settings(**defaults)


def _search(graph, buy_prices=None, sell_prices=None, **kwargs):
    defaults = dict(
        num_hops=1,
        starting_budget=100.0,
        ship_jump_range_gm=100.0,
        ship_cargo_capacity_scu=100.0,
        settings=_settings(),
    )
    defaults.update(kwargs)
    return find_best_route(
        graph,
        buy_prices=buy_prices or {},
        sell_prices=sell_prices or {},
        **defaults,
    )


# --- basic found / not-found -------------------------------------------------


def test_simple_profitable_edge_is_found():
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 100.0}}
    sell_prices = {2: {1: 150.0}}

    # cash=1000, buy=100 -> quantity=min(floor(1000/100)=10, cargo=50)=10
    # profit = 10 * (150 - 100) = 500 -> final_cash = 1500
    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=1,
        starting_budget=1000.0,
        ship_cargo_capacity_scu=50.0,
        ship_jump_range_gm=100.0,
    )

    assert result.found is True
    assert result.start_terminal_id == 1
    assert len(result.hops) == 1
    hop = result.hops[0]
    assert hop.terminal_id == 2
    assert hop.distance_from_previous == 10.0
    assert hop.commodity_id == 1
    assert hop.quantity_traded == 10.0
    assert hop.unit_buy_price == 100.0
    assert hop.unit_sell_price == 150.0
    assert hop.profit_this_hop == 500.0
    assert result.total_distance == 10.0
    assert result.total_profit == 500.0
    assert result.starting_budget == 1000.0
    assert result.final_cash == 1500.0


def test_isolated_start_node_returns_not_found_cleanly():
    graph = nx.DiGraph()
    graph.add_node(1)

    result = _search(graph, start_terminal_id=1, num_hops=3, starting_budget=1000.0)

    assert result.found is False
    assert result.hops == ()
    assert result.total_distance == 0.0
    assert result.total_profit == 0.0
    assert result.starting_budget == 1000.0
    assert result.final_cash == 1000.0
    assert result.message


def test_unknown_start_terminal_returns_not_found_not_crash():
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 100.0}}
    sell_prices = {2: {1: 150.0}}

    result = _search(graph, buy_prices, sell_prices, start_terminal_id=999, num_hops=1)

    assert result.found is False
    assert result.hops == ()


def test_all_neutral_bridge_hops_reports_not_found():
    # Every edge exists (still costs a hop) but nothing is profitable
    # anywhere reachable -- cash never moves, so this must not be reported
    # as a "found" wandering route.
    graph = _graph([(1, 2, 10.0), (2, 3, 10.0), (3, 1, 10.0)])

    result = _search(graph, start_terminal_id=1, num_hops=10, starting_budget=500.0)

    assert result.found is False
    assert result.final_cash == 500.0


# --- missing vs. zero price discipline ----------------------------------------


def test_missing_sell_price_excludes_commodity_not_treated_as_zero():
    # Commodity 1 has a buy price at terminal 1 but NO price entry at all
    # (not sell=0.0 -- genuinely absent) at terminal 2 -- must be excluded,
    # falling back to a neutral bridge hop, not a crash.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 100.0}}
    sell_prices: PricesByTerminal = {}  # no entry at all for terminal 2

    result = _search(graph, buy_prices, sell_prices, start_terminal_id=1, num_hops=1)

    assert result.found is False
    hop_free_chain_cash = result.final_cash
    assert hop_free_chain_cash == result.starting_budget


def test_zero_sell_price_is_valid_but_unprofitable_not_a_crash():
    # sell_price=0.0 at the destination is a real, known price (distinct
    # from "missing") -- it's simply worse than the buy price, so it must
    # lose to the neutral bridge hop, not crash the comparison.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 50.0}}
    sell_prices = {2: {1: 0.0}}

    result = _search(graph, buy_prices, sell_prices, start_terminal_id=1, num_hops=1)

    assert result.found is False


# --- cash-aware commodity/quantity selection (the core Phase 2 behavior) -----


def test_cheaper_lower_margin_commodity_wins_when_cash_limited():
    # Commodity 1: buy 10, sell 15 (margin 5). Commodity 2: buy 90, sell
    # 110 (margin 20 -- much higher per-unit, but only 1 unit affordable).
    # cash=100: qty_1 = min(floor(100/10)=10, 100) = 10 -> profit 50.
    #           qty_2 = min(floor(100/90)=1,  100) = 1  -> profit 20.
    # The cheaper commodity wins on *total* profit despite the worse margin.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0, 2: 90.0}}
    sell_prices = {2: {1: 15.0, 2: 110.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=1,
        starting_budget=100.0,
        ship_cargo_capacity_scu=100.0,
    )

    assert result.found is True
    hop = result.hops[0]
    assert hop.commodity_id == 1
    assert hop.quantity_traded == 10.0
    assert hop.unit_buy_price == 10.0
    assert hop.unit_sell_price == 15.0
    assert hop.profit_this_hop == 50.0
    assert result.final_cash == 150.0


def test_cargo_cap_limits_quantity_even_with_ample_cash():
    # cash is effectively unlimited (100,000); buy=10 -> cash alone would
    # afford 10,000 units, but cargo caps it at 7.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 20.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=1,
        starting_budget=100_000.0,
        ship_cargo_capacity_scu=7.0,
    )

    assert result.found is True
    hop = result.hops[0]
    assert hop.quantity_traded == 7.0
    assert hop.profit_this_hop == 70.0  # 7 * (20 - 10)
    assert result.final_cash == 100_070.0


def test_free_commodity_ignores_cash_uses_full_cargo_capacity():
    # buy_price == 0.0 -- quantity is defined as ship_cargo_capacity_scu
    # regardless of cash on hand (per spec: division by a non-positive buy
    # price is avoided by definition, not by luck). starting_budget=0.0
    # makes the "cash doesn't gate this" point unambiguous.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 0.0}}
    sell_prices = {2: {1: 5.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=1,
        starting_budget=0.0,
        ship_cargo_capacity_scu=12.0,
    )

    assert result.found is True
    hop = result.hops[0]
    assert hop.quantity_traded == 12.0
    assert hop.profit_this_hop == 60.0  # 12 * (5 - 0)
    assert result.final_cash == 60.0


def test_fractional_cash_to_quantity_uses_floor():
    # cash=105, buy=10 -> floor(105 / 10) = 10, not 10.5.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 12.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=1,
        starting_budget=105.0,
        ship_cargo_capacity_scu=100.0,
    )

    assert result.found is True
    assert result.hops[0].quantity_traded == 10.0


# --- ship jump range: hard per-hop distance filter ----------------------------


def test_edge_exceeding_ship_jump_range_is_never_taken():
    graph = _graph([(1, 2, 50.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 20.0}}

    result = _search(
        graph, buy_prices, sell_prices, start_terminal_id=1, num_hops=1, ship_jump_range_gm=10.0
    )

    assert result.found is False


def test_edge_at_exactly_ship_jump_range_is_allowed():
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 20.0}}

    result = _search(
        graph, buy_prices, sell_prices, start_terminal_id=1, num_hops=1, ship_jump_range_gm=10.0
    )

    assert result.found is True


# --- num_hops: bounds route length, not distance ------------------------------


def test_num_hops_bounds_route_length():
    # Chain 1->2->3->4, all profitable via the same commodity at every
    # terminal (buy 10 everywhere except the last, sell 20 everywhere
    # except the first). cargo cap 5 keeps the numbers simple/flat:
    #   hop1: cash=100 -> qty=min(10,5)=5 -> profit 50 -> cash=150 @ 2
    #   hop2: cash=150 -> qty=min(15,5)=5 -> profit 50 -> cash=200 @ 3
    # num_hops=2 stops here -- hop3 (3->4) is never taken.
    graph = _graph([(1, 2, 10.0), (2, 3, 10.0), (3, 4, 10.0)])
    buy_prices = {1: {1: 10.0}, 2: {1: 10.0}, 3: {1: 10.0}}
    sell_prices = {2: {1: 20.0}, 3: {1: 20.0}, 4: {1: 20.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=2,
        starting_budget=100.0,
        ship_cargo_capacity_scu=5.0,
    )

    assert result.found is True
    visited = [1] + [hop.terminal_id for hop in result.hops]
    assert visited == [1, 2, 3]
    assert len(result.hops) == 2
    assert result.total_distance == 20.0
    assert result.final_cash == 200.0
    assert result.total_profit == 100.0


# --- profitable cycle exploited across the available hops --------------------


def test_profitable_cycle_exploited_across_hops_and_terminates():
    # A profitable 2-cycle where quantity is always exactly cash/10 (never
    # cargo- or floor-limited): profit == cash each hop, so cash doubles
    # every hop. 100 -> 200 -> 400 -> 800 -> 1600 over 4 hops. This also
    # demonstrates the search structurally cannot hang on a positive cycle
    # (unlike Phase 1's flipped-comparator-Dijkstra risk) -- the DP is a
    # fixed loop over `num_hops`, not a frontier that could keep re-queuing
    # a cycle forever.
    graph = _graph([(1, 2, 5.0), (2, 1, 5.0)])
    buy_prices = {1: {1: 10.0}, 2: {1: 10.0}}
    sell_prices = {1: {1: 20.0}, 2: {1: 20.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=4,
        starting_budget=100.0,
        ship_cargo_capacity_scu=1_000_000.0,
        ship_jump_range_gm=100.0,
    )

    assert result.found is True
    assert len(result.hops) == 4
    visited = [1] + [hop.terminal_id for hop in result.hops]
    assert visited == [1, 2, 1, 2, 1]
    assert result.final_cash == 1600.0
    assert result.total_profit == 1500.0
    assert result.total_distance == 20.0


# --- best answer may need fewer than num_hops (dead end) ---------------------


def test_best_answer_found_at_fewer_hops_than_requested_dead_end():
    # Node 2 is a dead end (no outgoing edges) -- num_hops=5 is requested,
    # but the best (only) profitable answer is the single hop 1 -> 2.
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 20.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=5,
        starting_budget=100.0,
        ship_cargo_capacity_scu=5.0,
    )

    assert result.found is True
    assert len(result.hops) == 1
    assert result.hops[0].terminal_id == 2
    assert result.final_cash == 150.0  # 100 + 5*(20-10)


# --- the core simplification: best-cash-per-(node,hop), no Pareto frontier ---


def test_dp_keeps_higher_cash_label_when_multiple_predecessors_reach_same_node():
    # 1 -> 2 is a pure bridge (node 2 has no prices at all -- cash stays
    # 100). 1 -> 3 is profitable (commodity 1: buy 10 @ 1, sell 20 @ 3 ->
    # cash 100 -> 200). At hop 2, node 4 is reachable via 2 -> 4 (still a
    # bridge, cash stays 100) and via 3 -> 4 (commodity 2: buy 5 @ 3, sell
    # 15 @ 4 -> qty = min(floor(200/5)=40, 100) = 40, profit = 400, cash
    # 200 -> 600). The DP must keep the higher-cash arrival via 3 (600),
    # not the lower one via 2 (100) or the fewer-hops stop at 3 (200).
    graph = _graph([(1, 2, 5.0), (1, 3, 5.0), (2, 4, 5.0), (3, 4, 5.0)])
    buy_prices = {1: {1: 10.0}, 3: {2: 5.0}}
    sell_prices = {3: {1: 20.0}, 4: {2: 15.0}}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=2,
        starting_budget=100.0,
        ship_cargo_capacity_scu=100.0,
    )

    assert result.found is True
    visited = [1] + [hop.terminal_id for hop in result.hops]
    assert visited == [1, 3, 4]
    assert result.final_cash == 600.0
    assert result.total_profit == 500.0
    assert result.total_distance == 10.0


# --- invalid inputs handled gracefully -----------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("num_hops", 0),
        ("num_hops", -3),
        ("ship_jump_range_gm", 0.0),
        ("ship_jump_range_gm", -1.0),
        ("starting_budget", -5.0),
        ("ship_cargo_capacity_scu", -1.0),
    ],
)
def test_invalid_inputs_return_not_found_not_crash(field, value):
    graph = _graph([(1, 2, 10.0)])
    buy_prices = {1: {1: 10.0}}
    sell_prices = {2: {1: 20.0}}

    kwargs = {field: value}
    result = _search(graph, buy_prices, sell_prices, start_terminal_id=1, **kwargs)

    assert result.found is False
    assert result.hops == ()


# --- time budget: still returns a well-formed answer, doesn't crash ----------


def test_time_budget_exhaustion_still_returns_well_formed_result():
    edges = [(i, i + 1, 5.0) for i in range(1, 30)]
    graph = _graph(edges)
    buy_prices = {i: {1: 10.0} for i in range(1, 30)}
    sell_prices = {i: {1: 20.0} for i in range(2, 31)}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=29,
        starting_budget=100.0,
        ship_cargo_capacity_scu=1.0,
        settings=_settings(search_time_budget_seconds=0.0),
    )

    # Well-formed regardless of exactly how many hops completed before the
    # (already-elapsed) deadline was next checked: found<=>non-empty hops,
    # no crash, and at least the first hop's worth of work is guaranteed.
    if result.found:
        assert len(result.hops) >= 1
        assert result.total_distance > 0
        assert result.final_cash > result.starting_budget
    else:
        assert result.hops == ()


# --- deep-path label generation: parent-pointer reconstruction -----------------
#
# Regression coverage for the O(depth) `path + (successor,)` tuple-copy bug
# class (see `backend/graph/search.py`'s module docstring, "Path
# reconstruction" section): label creation must stay cheap regardless of how
# deep the search goes, and the single winning label's path must still be
# reconstructed correctly -- in the right order, starting from the start
# node -- from the parent-pointer chain.


def test_very_long_path_reconstructed_correctly_and_in_order():
    # A long, unbranching chain, cargo-capped to exactly 1 unit so profit
    # is a flat +1 cash every hop (1 * (2 - 1)) -- easy to hand-verify at
    # any depth: final_cash == starting_budget + (chain_length - 1).
    chain_length = 1500
    edges = [(i, i + 1, 1.0) for i in range(1, chain_length)]
    graph = _graph(edges)
    buy_prices = {i: {1: 1.0} for i in range(1, chain_length)}
    sell_prices = {i: {1: 2.0} for i in range(2, chain_length + 1)}

    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=1,
        num_hops=chain_length - 1,
        starting_budget=1000.0,
        ship_cargo_capacity_scu=1.0,
        ship_jump_range_gm=10.0,
    )

    assert result.found is True
    assert len(result.hops) == chain_length - 1
    visited = [1] + [hop.terminal_id for hop in result.hops]
    assert visited == list(range(1, chain_length + 1))
    assert result.final_cash == 1000.0 + (chain_length - 1)
    assert result.total_profit == chain_length - 1


def test_deep_branching_cyclic_search_stays_fast_and_well_formed():
    # A smaller, fast-running cousin of
    # tests/perf/test_perf.py::test_search_perf_on_large_dense_graph --
    # dense, cyclic, branching (not a straight chain), so the DP's
    # best-cash-per-(node,hop) merge logic is exercised across many
    # converging predecessors, not just a single-path chain. Runs on every
    # default `pytest` invocation (unlike the perf-marked test) at a scale
    # small enough to stay fast but deep enough to exercise real recursion
    # depth in path reconstruction.
    rng = random.Random(20260815)
    node_count = 150
    out_degree = 10
    edges = []
    terminal_price_level: dict[int, float] = {}
    for node in range(node_count):
        successors = rng.sample([n for n in range(node_count) if n != node], k=out_degree)
        for successor in successors:
            distance = float(rng.randint(1, 5))
            edges.append((node, successor, distance))
        # A single global commodity (id 1); each terminal's buy/sell price
        # is the same random "level" -- an edge is profitable exactly when
        # the destination's level exceeds the origin's, which happens
        # often enough across 150 random levels to create plenty of
        # profitable cycles without any special-casing.
        terminal_price_level[node] = float(rng.randint(1, 20))
    graph = _graph(edges)
    buy_prices = {node: {1: level} for node, level in terminal_price_level.items()}
    sell_prices = {node: {1: level} for node, level in terminal_price_level.items()}

    started = time.monotonic()
    result = _search(
        graph,
        buy_prices,
        sell_prices,
        start_terminal_id=0,
        num_hops=100,
        starting_budget=1000.0,
        ship_cargo_capacity_scu=1_000_000.0,
        ship_jump_range_gm=1000.0,
        settings=_settings(search_time_budget_seconds=5.0),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"find_best_route() took {elapsed:.2f}s on a {node_count}-node graph"
    assert result.found is True
    assert len(result.hops) >= 50

    # Every reported hop must correspond to a real edge in the graph.
    path = [0] + [hop.terminal_id for hop in result.hops]
    for origin, destination in zip(path, path[1:]):
        assert graph.has_edge(origin, destination)
