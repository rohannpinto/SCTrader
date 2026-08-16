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

Phase 2: `build_graph()` no longer computes a per-edge `weight`/`profit`/
`best_commodity_id` (that decision moved to `backend/graph/search.py`,
which is cash-aware at query time) -- edges carry only `distance`, and the
bulk `buy_prices`/`sell_prices` indices ride on `GraphBuildResult` instead
of being consumed internally. See `backend/graph/builder.py`'s module
docstring for the full rationale.
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
    defaults: dict = dict(graph_edge_count_guardrail=50000)
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


# --- edges carry only `distance` (Phase 2) ------------------------------------


def _seed_two_terminals_and_commodities(session):
    """Parent rows only -- see module docstring's seeding convention."""
    session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    session.add(Terminal(id=2, name="B", is_commodity_trading=True))
    session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    session.add(Commodity(id=2, wiki_uuid="u2", slug="agricium", name="Agricium"))


def test_edge_carries_only_distance_attribute(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=300.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    edge = result.graph.edges[1, 2]
    assert edge == {"distance": 10.0}
    assert "weight" not in edge
    assert "profit" not in edge
    assert "best_commodity_id" not in edge


def test_edge_distance_is_stored_raw_never_floored(engine):
    # Phase 2: builder.py no longer applies `min_distance_floor` at all --
    # that responsibility moved entirely to ingestion time
    # (`backend/ingest/refresh.py`, already covered by
    # `test_refresh.py::test_same_orbit_terminal_pairs_get_min_distance_floor`).
    # A raw zero distance (however it got into the DB) is stored as-is.
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=0.0, fetched_at=_now()))

    result = build_graph(engine=engine, settings=Settings(min_distance_floor=2.5))

    assert result.graph.edges[1, 2]["distance"] == 0.0


def test_edge_exists_regardless_of_whether_any_commodity_is_profitable(engine):
    # No Price rows at all between 1 and 2 -- the edge still exists (a
    # potential "bridge hop" -- profitability is entirely `search.py`'s
    # concern now, at query time).
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))

    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.has_edge(1, 2)
    assert result.graph.edges[1, 2] == {"distance": 10.0}


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


# --- commodity display names --------------------------------------------------


def test_commodity_names_are_bulk_loaded_for_display(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    result = build_graph(engine=engine, settings=_settings())

    assert result.commodity_names == {1: "Laranite", 2: "Agricium"}


# --- buy_prices / sell_prices bulk indices (Phase 2) --------------------------


def test_buy_and_sell_price_indices_are_bulk_loaded(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=1, commodity_id=2, price_buy=50.0, price_sell=75.0, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    assert result.buy_prices == {1: {1: 100.0, 2: 50.0}}
    assert result.sell_prices == {2: {1: 200.0}, 1: {2: 75.0}}


def test_missing_price_is_absent_from_index_not_stored_as_zero(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        # Only a buy price at terminal 1 -- no Price row at all for
        # terminal 2 / commodity 1.
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    assert result.buy_prices == {1: {1: 100.0}}
    assert result.sell_prices == {}
    assert 2 not in result.sell_prices


def test_zero_price_is_present_in_index_distinct_from_missing(engine):
    with session_scope(engine) as session:
        _seed_two_terminals_and_commodities(session)

    with session_scope(engine) as session:
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=0.0, price_sell=None, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    # A real, known price of 0.0 is present in the index (a valid dict
    # entry), distinct from "no entry at all" for a different terminal.
    assert result.buy_prices == {2: {1: 0.0}}
    assert 1 not in result.buy_prices


def test_price_row_touching_non_commodity_terminal_is_skipped(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="Ship Dealer", is_commodity_trading=False))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))

    with session_scope(engine) as session:
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )

    result = build_graph(engine=engine, settings=_settings())

    assert result.buy_prices == {}


# --- empty DB -----------------------------------------------------------------


def test_empty_database_yields_empty_graph(engine):
    result = build_graph(engine=engine, settings=_settings())

    assert result.graph.number_of_nodes() == 0
    assert result.graph.number_of_edges() == 0
    assert result.commodity_names == {}
    assert result.buy_prices == {}
    assert result.sell_prices == {}
    assert result.warnings == []
