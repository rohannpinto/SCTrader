"""Synthetic large-graph performance benchmarks.

Excluded from the default suite (`pytest -m perf` to run explicitly, per
`pyproject.toml`) -- these are regression guards against gross algorithmic
mistakes (an accidental N+1 query, an accidental O(n^2) Python loop, the
label-setting search's safety valves silently not firing), not strict
timing assertions. Bounds are deliberately generous so these stay reliable
on a slow CI/dev machine while still catching a real regression (e.g. a
search that completely ignores `search_time_budget_seconds` would blow
even a generous bound by a wide margin).
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
    this is test *setup*, not part of what `test_build_graph_perf` times.
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
            # exercise the per-edge max-profit-commodity scan realistically
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

    # Regression guard against an accidental N+1 query or O(n^2) Python-level
    # loop, not a strict performance SLA -- still generous, but tightened from
    # an earlier 15.0s bound. An independent review measured this build at
    # ~0.25-0.3s on real hardware; a per-terminal N+1 query pattern (300
    # queries) still only reached ~0.3s (SQLite's per-query overhead is too
    # low at this scale for a single N+1 pass to trip a loose bound), but a
    # severe per-edge N+1 pattern (~36,000 extra queries) reached ~10.7s.
    # 5.0s (~15-20x the measured baseline) catches that class of regression
    # while staying safely clear of normal variance on a slower machine.
    assert elapsed < 5.0, f"build_graph() took {elapsed:.2f}s on a {terminal_count}-terminal dataset"


# --- label-setting search: anytime behavior under load -----------------------


def _large_synthetic_graph(*, node_count: int, out_degree: int) -> nx.DiGraph:
    """A dense, cyclic graph with a mix of profitable and zero-weight edges
    -- deliberately adversarial for the search (profitable cycles are
    exactly what CLAUDE.md warns a flipped-comparator Dijkstra would loop
    forever on).
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
            # ~30% of edges profitable, to guarantee plenty of
            # positive-weight cycles for the search to (correctly) not get
            # stuck exploiting forever.
            weight = float(rng.randint(1, 20)) if rng.random() < 0.3 else 0.0
            graph.add_edge(
                node, successor, distance=distance, weight=weight, profit=weight * distance,
                best_commodity_id=1 if weight > 0 else None,
            )
    return graph


def test_search_respects_time_budget_on_large_dense_graph():
    graph = _large_synthetic_graph(node_count=2000, out_degree=25)
    settings = Settings(search_time_budget_seconds=2.0, search_label_cap_per_node=50)

    started = time.monotonic()
    result = find_best_route(
        graph,
        start_terminal_id=0,
        max_distance=1_000_000.0,  # budget alone would allow a huge number of hops
        distance_threshold=1000.0,
        settings=settings,
    )
    elapsed = time.monotonic() - started

    # Generous slack above the configured budget: the deadline is only
    # checked once per label popped off the heap, so a single expansion
    # phase can run slightly over -- this bound exists to catch the budget
    # being ignored entirely (e.g. an infinite loop on a positive-weight
    # cycle), not to enforce the budget to the millisecond.
    assert elapsed < settings.search_time_budget_seconds + 5.0, (
        f"find_best_route() took {elapsed:.2f}s against a "
        f"{settings.search_time_budget_seconds}s budget"
    )
    # A well-formed answer regardless of outcome -- the anytime guarantee.
    if result.found:
        assert len(result.hops) >= 1
        assert result.total_distance <= 1_000_000.0
    else:
        assert result.hops == ()
