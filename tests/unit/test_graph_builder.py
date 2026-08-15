"""Unit tests for `backend/graph/builder.py`.

All tests use a temp *file* SQLite database (matches `test_db_models.py`'s
convention) seeded directly via the ORM -- no HTTP/respx involved, since
this module never talks to an external API, only the cache DB.

Seeding convention: parent rows (`Terminal`, `Commodity`) are always
committed in their own `session_scope` block *before* child rows
(`Distance`, `Price`) are added in a separate block. These models use plain
FK columns with no `relationship()` mapping, so SQLAlchemy's unit-of-work
has no cross-class dependency info to auto-order a single mixed flush by --
mirrors the same convention already used in `test_db_models.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.config import Settings
from backend.graph.builder import build_graph
from backend.models.db import (
    Commodity,
    Distance,
    Price,
    Terminal,
    create_db_engine,
    init_db,
    session_scope,
)


@pytest.fixture
def engine(tmp_path):
    db_file = tmp_path / "test_graph.db"
    eng = create_db_engine(str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> Settings:
    defaults: dict = dict(min_distance_floor=1.0, graph_edge_count_guardrail=50000)
    defaults.update(overrides)
    return Settings(**defaults)


# --- node set ---------------------------------------------------------------


def test_only_commodity_trading_terminals_become_nodes(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Trade Post", is_commodity_trading=True))
        session.add(Terminal(id=2, name="Ship Dealer", is_commodity_trading=False))

    result = build_graph(engine=engine, settings=_settings())

    assert set(result.graph.nodes) == {1}


def test_isolated_commodity_terminal_is_still_a_node(engine):
    # No Distance rows at all -- must still appear as a zero-edge node so a
    # router can tell "unknown terminal" apart from "known but isolated".
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Lonely Outpost", is_commodity_trading=True))

    result = build_graph(engine=engine, settings=_settings())

    assert list(result.graph.nodes) == [1]
    assert result.graph.number_of_edges() == 0


def test_node_attributes_carry_display_metadata(engine):
    with session_scope(engine) as session:
        session.add(
            Terminal(
                id=1,
                name="Stanton Gateway",
                code="STANTG",
                star_system_name="Nyx",
                location_name="Levski",
                is_commodity_trading=True,
            )
        )

    result = build_graph(engine=engine, settings=_settings())

    attrs = result.graph.nodes[1]
    assert attrs["name"] == "Stanton Gateway"
    assert attrs["code"] == "STANTG"
    assert attrs["star_system_name"] == "Nyx"
    assert attrs["location_name"] == "Levski"


# --- weight formula: best commodity picked per edge -------------------------


def _seed_two_terminals_and_commodities(session):
    """Parent rows only -- see module docstring's seeding convention."""
    session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    session.add(Terminal(id=2, name="B", is_commodity_trading=True))
    session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    session.add(Commodity(id=2, wiki_uuid="u2", slug="agricium", name="Agricium"))


def test_weight_picks_max_profit_commodity_across_candidates(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        # Laranite: buy 100 @ A, sell 150 @ B -> profit 50
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=150.0, fetched_at=_now())
        )
        # Agricium: buy 100 @ A, sell 300 @ B -> profit 200 (should win)
        session.add(
            Price(terminal_id=1, commodity_id=2, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=2, price_buy=None, price_sell=300.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    edge = result.graph.edges[1, 2]
    assert edge["best_commodity_id"] == 2
    assert edge["profit"] == 200.0
    assert edge["weight"] == pytest.approx(200.0 / 10.0)
    assert edge["distance"] == 10.0


def test_missing_price_excludes_commodity_not_treated_as_zero(engine):
    # Commodity 1 has a buy price at A but NO sell price row at B at all
    # (not price_sell=0.0 -- genuinely absent) -- must be excluded from
    # consideration entirely, not scored as a -100 loss (which would still
    # correctly lose to weight=0, but must not raise/crash either).
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        # No Price row at all for terminal 2 / commodity 1.

    result = build_graph(engine=engine, settings=_settings())

    edge = result.graph.edges[1, 2]
    assert edge["best_commodity_id"] is None
    assert edge["weight"] == 0.0
    assert edge["profit"] == 0.0


def test_zero_price_is_a_valid_candidate_distinct_from_missing(engine):
    # price_sell=0.0 at the destination is a real, known price (e.g. a
    # sell-only terminal has price_buy=0) -- profit here is negative, so
    # this commodity loses to weight=0, but it must not be confused with
    # "missing" and must not crash the comparison.
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=50.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=0.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    edge = result.graph.edges[1, 2]
    assert edge["best_commodity_id"] is None
    assert edge["weight"] == 0.0


def test_unprofitable_edge_still_exists_with_zero_weight(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        # Loss on the only candidate commodity: buy 200 @ A, sell 100 @ B.
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=200.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=100.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.has_edge(1, 2)
    edge = result.graph.edges[1, 2]
    assert edge["weight"] == 0.0
    assert edge["best_commodity_id"] is None


# --- min_distance_floor ------------------------------------------------------


def test_zero_distance_uses_min_distance_floor_as_denominator(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=0.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=150.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings(min_distance_floor=2.5))

    edge = result.graph.edges[1, 2]
    # distance attribute stays the raw, un-floored value (real in-game
    # distance for reporting) -- only the weight denominator is floored.
    assert edge["distance"] == 0.0
    assert edge["weight"] == pytest.approx(50.0 / 2.5)


def test_distance_below_floor_still_uses_floor(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=0.4, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=0.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=10.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings(min_distance_floor=1.0))

    edge = result.graph.edges[1, 2]
    assert edge["weight"] == pytest.approx(10.0 / 1.0)  # floored to 1.0, not 0.4


# --- directionality -----------------------------------------------------------


def test_edges_are_directed_not_symmetric(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))

    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.has_edge(1, 2)
    assert not result.graph.has_edge(2, 1)


# --- non-commodity terminal referenced by a stray Distance row --------------


def test_distance_row_touching_non_commodity_terminal_is_skipped(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="Ship Dealer", is_commodity_trading=False))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))

    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.number_of_edges() == 0
    assert set(result.graph.nodes) == {1}


# --- guardrail ------------------------------------------------------------


def test_edge_count_guardrail_adds_warning_but_still_returns_full_graph(engine):
    with session_scope(engine) as session:
        for i in range(1, 5):
            session.add(Terminal(id=i, name=f"T{i}", is_commodity_trading=True))

    with session_scope(engine) as session:
        # 4 terminals -> up to 12 directed pairs; guardrail set to 5 so this
        # trips the warning without needing a huge fixture.
        for a in range(1, 5):
            for b in range(1, 5):
                if a != b:
                    session.add(
                        Distance(terminal_a_id=a, terminal_b_id=b, distance=10.0, fetched_at=_now())
                    )

    result = build_graph(engine=engine, settings=_settings(graph_edge_count_guardrail=5))

    assert result.graph.number_of_edges() == 12
    assert len(result.warnings) == 1
    assert "12" in result.warnings[0]


def test_no_warning_when_under_guardrail(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))

    result = build_graph(engine=engine, settings=_settings(graph_edge_count_guardrail=50000))

    assert result.warnings == []


# --- empty DB -----------------------------------------------------------------


def test_empty_database_yields_empty_graph(engine):
    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.number_of_nodes() == 0
    assert result.graph.number_of_edges() == 0
    assert result.warnings == []
