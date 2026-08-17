"""Synthetic large-graph performance benchmarks.

Excluded from the default suite (`pytest -m perf` to run explicitly, per
`pyproject.toml`) -- these are regression guards against gross algorithmic
mistakes (an accidental N+1 query, an accidental O(n^2) Python loop, the
search's time-budget safety valve silently not firing), not strict timing
assertions. Bounds are deliberately generous so these stay reliable on a
slow CI/dev machine while still catching a real regression.

Phase 2: `test_search_perf_on_large_dense_graph` replaces Phase 1's
distance-budget/label-cap benchmark -- the new search has no label cap to
exercise (the whole point of the Phase 2 redesign is that the state space,
`O(nodes * num_hops)`, is small and exactly bounded without one -- see
`backend/graph/search.py`'s module docstring), so this benchmarks the
bounded DP directly at a size well beyond anything Star Citizen's real
terminal count would produce.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

import networkx as nx
import pytest

from backend.config import Settings
from backend.graph.builder import build_graph
from backend.graph.search import find_best_route
from backend.models.db import (
    Commodity,
    Distance,
    Price,
    Terminal,
    create_db_engine,
    get_session_factory,
    init_db,
)

pytestmark = pytest.mark.perf

_RNG_SEED = 20260814


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def engine(tmp_path):
    db_file = tmp_path / "perf_test.db"
    eng = create_db_engine(str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


def _seed_large_dataset(
    engine, *, terminal_count: int, out_degree: int, commodity_count: int
) -> None:
    """Bulk-inserts a large, randomly-connected dataset directly via
    `bulk_insert_mappings` (plain dicts, no per-row ORM object construction
    or Python-level looping in the thing actually being benchmarked) --
    this is test *setup*, not part of what `test_build_graph_perf_on_large_dataset` times.
    """
    rng = random.Random(_RNG_SEED)
    session_factory = get_session_factory(engine)
    session = session_factory()
    try:
        session.bulk_insert_mappings(
            Terminal,
            [
                {"id": i, "name": f"Terminal {i}", "is_commodity_trading": True}
                for i in range(1, terminal_count + 1)
            ],
        )
        session.bulk_insert_mappings(
            Commodity,
            [
                {"id": i, "wiki_uuid": f"uuid-{i}", "slug": f"commodity-{i}", "name": f"Commodity {i}"}
                for i in range(1, commodity_count + 1)
            ],
        )
        session.commit()

        fetched_at = _now()
        distance_rows = []
        price_rows = []
        for terminal_id in range(1, terminal_count + 1):
            others = rng.sample(
                [t for t in range(1, terminal_count + 1) if t != terminal_id],
                k=min(out_degree, terminal_count - 1),
            )
            for other_id in others:
                distance_rows.append(
                    {
                        "terminal_a_id": terminal_id,
                        "terminal_b_id": other_id,
                        "distance": float(rng.randint(10, 1000)),
                        "fetched_at": fetched_at,
                    }
                )
            # A handful of commodities priced at every terminal -- enough to
            # exercise the per-edge candidate-commodity scan realistically
            # without needing a full price matrix.
            for commodity_id in range(1, commodity_count + 1):
                price_rows.append(
                    {
                        "terminal_id": terminal_id,
                        "commodity_id": commodity_id,
                        "price_buy": float(rng.randint(1, 500)),
                        "price_sell": float(rng.randint(1, 500)),
                        "source_date_updated": None,
                        "fetched_at": fetched_at,
                    }
                )

        session.bulk_insert_mappings(Distance, distance_rows)
        session.bulk_insert_mappings(Price, price_rows)
        session.commit()
    finally:
        session.close()


# --- graph builder: bulk-load performance ------------------------------------


def test_build_graph_perf_on_large_dataset(engine):
    terminal_count = 300
    out_degree = 60  # ~18,000 directed edges -- comfortably above the guardrail path
    _seed_large_dataset(engine, terminal_count=terminal_count, out_degree=out_degree, commodity_count=15)

    settings = Settings(graph_edge_count_guardrail=5000)  # deliberately low: exercise the warning path

    started = time.monotonic()
    result = build_graph(engine=engine, settings=settings)
    elapsed = time.monotonic() - started

    assert result.graph.number_of_nodes() == terminal_count
    assert result.graph.number_of_edges() == terminal_count * out_degree
    assert len(result.warnings) == 1  # edge count exceeds the deliberately-low guardrail
    # Phase 2: buy_prices/sell_prices are bulk-loaded and returned instead of
    # being consumed into a per-edge weight -- confirm they're actually
    # populated at this scale too, not just fast.
    assert len(result.buy_prices) == terminal_count
    assert len(result.sell_prices) == terminal_count

    # Regression guard against an accidental N+1 query or O(n^2) Python-level
    # loop, not a strict performance SLA. Freshly measured on this machine
    # (2026-08-15, Phase 2 Task 14), 3 consecutive runs: 0.125s, 0.156s,
    # 0.140s -- build_graph() (now doing strictly less per-edge work than
    # Phase 1's version, since there's no per-edge max-profit-commodity scan
    # anymore -- just three bulk queries and a `graph.add_edge()` loop).
    # 5.0s (~30x the measured baseline) catches an N+1/brute-force-style
    # regression while staying safely clear of normal variance on a slower
    # machine.
    assert elapsed < 5.0, f"build_graph() took {elapsed:.2f}s on a {terminal_count}-terminal dataset"


# --- bounded DP search: performance at a large, dense, cyclic state space ----


def _large_synthetic_graph_and_prices(
    *, node_count: int, out_degree: int
) -> tuple[nx.DiGraph, dict[int, dict[int, float]], dict[int, dict[int, float]]]:
    """A dense, cyclic graph (deliberately adversarial for a search that
    revisits nodes) plus `buy_prices`/`sell_prices` indices built the same
    "single global commodity, random price level per terminal" way as
    `tests/unit/test_search.py`'s `test_deep_branching_cyclic_search_
    stays_fast_and_well_formed` -- profitable whenever a hop's destination
    has a higher price level than its origin, which happens often enough
    across many random levels to create plenty of profitable cycles.
    """
    rng = random.Random(_RNG_SEED)
    graph = nx.DiGraph()
    graph.add_nodes_from(range(node_count))
    for node in range(node_count):
        successors = rng.sample(
            [n for n in range(node_count) if n != node], k=min(out_degree, node_count - 1)
        )
        for successor in successors:
            distance = float(rng.randint(5, 50))
            graph.add_edge(node, successor, distance=distance)

    price_level = {node: float(rng.randint(1, 100)) for node in range(node_count)}
    buy_prices = {node: {1: level} for node, level in price_level.items()}
    sell_prices = {node: {1: level} for node, level in price_level.items()}
    return graph, buy_prices, sell_prices


def test_search_perf_on_large_dense_graph():
    # 300 terminals, out-degree 60 (matching `test_build_graph_perf_on_
    # large_dataset`'s density -- comfortably beyond the real 161-terminal
    # data volume) for 200 hops: a state space of 300 * 200 = 60,000
    # (node, hop) pairs, each considering up to 60 outgoing edges -- ~3.6
    # million edge relaxations total. A generous time budget (well beyond
    # what this needs) so the search runs to true completion rather than
    # being cut off by the deadline -- `test_search_respects_time_budget_
    # on_large_dense_graph` below covers the cut-off-by-deadline case
    # separately.
    node_count = 300
    out_degree = 60
    num_hops = 200
    graph, buy_prices, sell_prices = _large_synthetic_graph_and_prices(
        node_count=node_count, out_degree=out_degree
    )
    settings = Settings(search_time_budget_seconds=120.0)

    started = time.monotonic()
    result = find_best_route(
        graph,
        start_terminal_id=0,
        num_hops=num_hops,
        starting_budget=1000.0,
        ship_jump_range_gm=1000.0,  # generous -- the distance filter is not the thing under test
        ship_cargo_capacity_scu=1_000_000.0,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        settings=settings,
    )
    elapsed = time.monotonic() - started

    # Freshly measured on this machine (2026-08-15, Phase 2 Task 14),
    # 4 consecutive runs: 4.20s, 4.20s, 4.22s, 4.23s -- the bounded DP over
    # a 300-node, out-degree-60 graph for a full 200 hops to completion
    # (no priority queue, no label cap, no dominance bookkeeping, just a
    # fixed double loop over hop levels). 20.0s (~5x the measured baseline)
    # is generous enough to stay reliable on a slower/loaded machine while
    # still catching a real regression (e.g. an accidental O(depth) path
    # reconstruction on every candidate instead of only the winner, or an
    # accidental per-hop DB/graph re-query).
    assert elapsed < 20.0, (
        f"find_best_route() took {elapsed:.2f}s on a {node_count}-node, "
        f"{num_hops}-hop search"
    )
    assert result.found is True
    assert len(result.hops) == num_hops
    assert result.final_cash > result.starting_budget


def test_search_perf_on_large_dense_graph_rank10():
    # Phase 3: the same 300-node, out-degree-60 graph as
    # `test_search_perf_on_large_dense_graph` above, but exercising
    # `_find_top_k_routes` (rank=10) instead of `_find_single_best_route`
    # (rank=1) -- the top-K bookkeeping (retaining/merging/re-sorting up to
    # `rank` labels per (node, hop) instead of just 1, and trying every
    # outgoing edge from every one of those retained labels instead of only
    # the single best) is real additional work, bounded by `nodes *
    # num_hops * rank * out_degree` candidate labels considered per the
    # module docstring's "Phase 3" section.
    #
    # Deliberately uses num_hops=50 here, not 200 like the rank=1 benchmark
    # above -- freshly measured (2026-08-16) at num_hops=200, rank=10 took
    # ~107s (20.33s/47.55s/107.22s at 50/100/200 hops respectively, roughly
    # linear in num_hops as expected), which is too slow for a perf test
    # meant to be run repeatedly during iteration even though `-m perf` is
    # already opt-in/excluded from the default suite. num_hops=50 still
    # produces a large retained state space (300 * 50 * 10 = 150,000
    # retained labels, the same order of magnitude as the rank=1 20,000-
    # hop... i.e. 300 * 200 = 60,000-(node,hop)-pair benchmark above) while
    # keeping the benchmark itself practical to run often.
    #
    # Freshly measured on this machine (2026-08-16, Phase 3 Task 20), 3
    # consecutive runs at num_hops=50: 20.78s, 20.52s, 20.45s -- roughly
    # 5x slower than the rank=1 DP would take at the same 50-hop depth
    # (consistent with rank=10's ~10x more retained labels per state,
    # partially offset by the fact that fewer than out_degree=60 successors
    # end up novel per destination once results converge). 90.0s (~4.4x the
    # measured baseline) is generous enough to stay reliable on a slower/
    # loaded machine while still catching a real regression (e.g. an
    # accidental O(depth) path materialization per candidate instead of
    # only the final winner, or truncating per-edge instead of merging all
    # of a state's predecessors before truncating).
    node_count = 300
    out_degree = 60
    num_hops = 50
    graph, buy_prices, sell_prices = _large_synthetic_graph_and_prices(
        node_count=node_count, out_degree=out_degree
    )
    settings = Settings(search_time_budget_seconds=120.0)

    started = time.monotonic()
    result = find_best_route(
        graph,
        start_terminal_id=0,
        num_hops=num_hops,
        starting_budget=1000.0,
        ship_jump_range_gm=1000.0,  # generous -- the distance filter is not the thing under test
        ship_cargo_capacity_scu=1_000_000.0,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        settings=settings,
        rank=10,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 90.0, (
        f"find_best_route(rank=10) took {elapsed:.2f}s on a {node_count}-node, "
        f"{num_hops}-hop search"
    )
    assert result.found is True
    assert result.requested_rank == 10
    assert result.actual_rank_used == 10  # this dense/cyclic graph has well over 10 distinct profitable routes
    assert result.final_cash > result.starting_budget


def test_search_respects_time_budget_on_large_dense_graph():
    # A tighter time budget than the graph would naturally finish within --
    # confirms the deadline check actually fires and still returns a
    # well-formed "best found so far" answer rather than running to
    # completion regardless or crashing. Freshly measured on this machine
    # (2026-08-15): a 0.05s budget against this graph completes ~4-5 hop
    # levels (~0.06s wall clock) before the deadline check stops it, far
    # short of the 2000 requested.
    node_count = 300
    out_degree = 60
    graph, buy_prices, sell_prices = _large_synthetic_graph_and_prices(
        node_count=node_count, out_degree=out_degree
    )
    settings = Settings(search_time_budget_seconds=0.05)

    started = time.monotonic()
    result = find_best_route(
        graph,
        start_terminal_id=0,
        num_hops=2000,  # far more hops than a 0.05s budget can fully compute
        starting_budget=1000.0,
        ship_jump_range_gm=1000.0,
        ship_cargo_capacity_scu=1_000_000.0,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        settings=settings,
    )
    elapsed = time.monotonic() - started

    # Generous slack above the configured budget: the deadline is only
    # checked once per fully-completed hop level, so a single hop's worth of
    # work (up to node_count * out_degree edge relaxations) can run slightly
    # over -- this bound exists to catch the budget being ignored entirely,
    # not to enforce it to the millisecond.
    assert elapsed < settings.search_time_budget_seconds + 5.0, (
        f"find_best_route() took {elapsed:.2f}s against a "
        f"{settings.search_time_budget_seconds}s budget"
    )
    if result.found:
        assert len(result.hops) >= 1
        assert len(result.hops) < 2000  # the deadline must have cut this well short
    else:
        assert result.hops == ()


def test_search_respects_time_budget_on_large_dense_graph_rank10():
    # Phase 3 regression guard: the same tight-deadline scenario as
    # `test_search_respects_time_budget_on_large_dense_graph` above, but
    # exercising `_find_top_k_routes` (rank=10) instead of
    # `_find_single_best_route` (rank=1) -- confirms the anytime safety
    # valve (`settings.search_time_budget_seconds`, checked once between
    # fully-computed hop levels) genuinely fires on the top-K path too, not
    # only the rank=1 path, and still returns a well-formed "best found so
    # far" answer rather than running unconditionally to completion or
    # crashing. This is a real, separate code path from rank=1
    # (`_find_top_k_routes` has its own deadline check and its own loop),
    # so rank=1 passing this doesn't already prove rank=10 does.
    #
    # Freshly measured on this machine (2026-08-16, Phase 3 Task 20), 3
    # consecutive runs against a 0.05s budget: 0.281s, 0.312s, 0.313s --
    # only 3 hop levels complete before the deadline check stops it (each
    # rank=10 hop level does more work than a rank=1 one -- more retained
    # labels per state, each trying every outgoing edge, plus the top-K
    # re-sort -- so fewer hop levels finish in the same wall-clock window
    # than the rank=1 version manages, but the deadline still fires
    # promptly rather than being ignored).
    node_count = 300
    out_degree = 60
    graph, buy_prices, sell_prices = _large_synthetic_graph_and_prices(
        node_count=node_count, out_degree=out_degree
    )
    settings = Settings(search_time_budget_seconds=0.05)

    started = time.monotonic()
    result = find_best_route(
        graph,
        start_terminal_id=0,
        num_hops=2000,  # far more hops than a 0.05s budget can fully compute
        starting_budget=1000.0,
        ship_jump_range_gm=1000.0,
        ship_cargo_capacity_scu=1_000_000.0,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        settings=settings,
        rank=10,
    )
    elapsed = time.monotonic() - started

    # Same generous slack rationale as the rank=1 version above -- the
    # deadline is only checked once per fully-completed hop level, and a
    # rank=10 hop level does more work per level than rank=1's, but the
    # freshly-measured ~0.28-0.31s overshoot is still tiny in absolute
    # terms, so the same +5.0s slack comfortably covers a slower/loaded
    # machine too.
    assert elapsed < settings.search_time_budget_seconds + 5.0, (
        f"find_best_route(rank=10) took {elapsed:.2f}s against a "
        f"{settings.search_time_budget_seconds}s budget"
    )
    if result.found:
        assert len(result.hops) >= 1
        assert len(result.hops) < 2000  # the deadline must have cut this well short
    else:
        assert result.hops == ()
