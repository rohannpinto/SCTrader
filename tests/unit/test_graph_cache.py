"""Unit tests for `backend/graph/cache.py`.

Uses a temp-file SQLite DB seeded directly via the ORM (same convention as
`test_graph_builder.py`) plus `GraphCache` instantiated directly -- not the
process-wide `get_graph_cache()` singleton, so tests never leak state into
each other.

Phase 2: `GraphCacheSnapshot` gains `buy_prices`/`sell_prices` (the bulk
price indices `backend/graph/builder.py` now returns instead of consuming
internally) -- covered below alongside the existing atomic-swap guarantees.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.config import Settings
from backend.graph.cache import GraphCache, get_graph_cache
from backend.models.db import (
    Commodity,
    Price,
    RefreshRun,
    Terminal,
    create_db_engine,
    init_db,
    session_scope,
)


@pytest.fixture
def engine(tmp_path):
    db_file = tmp_path / "test_graph_cache.db"
    eng = create_db_engine(str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


# --- initial state (before any rebuild) --------------------------------------


def test_initial_state_is_empty_graph_no_version_no_warnings():
    cache = GraphCache()

    graph = cache.get_graph()
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0
    assert cache.get_data_version() is None
    assert cache.get_warnings() == []
    assert cache.get_buy_prices() == {}
    assert cache.get_sell_prices() == {}


# --- rebuild builds from whatever is on disk ----------------------------------


def test_rebuild_on_empty_db_yields_empty_graph_and_none_version(engine):
    cache = GraphCache()

    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert snapshot.graph.number_of_nodes() == 0
    assert snapshot.data_version is None
    assert cache.get_graph() is snapshot.graph
    assert cache.get_data_version() is None


def test_rebuild_reflects_seeded_terminals(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert set(snapshot.graph.nodes) == {1, 2}
    assert set(cache.get_graph().nodes) == {1, 2}


def test_rebuild_picks_up_most_recent_successful_refresh_run_id(engine):
    with session_scope(engine) as session:
        session.add(RefreshRun(started_at=_now(), completed_at=_now(), status="failed"))
        session.add(RefreshRun(started_at=_now(), completed_at=_now(), status="success"))

    with session_scope(engine) as session:
        expected_version = session.query(RefreshRun).filter_by(status="success").one().id

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert snapshot.data_version == expected_version
    assert cache.get_data_version() == expected_version


def test_rebuild_with_no_successful_run_but_real_data_still_builds_graph(engine):
    # Terminal data can exist even with no RefreshRun row at all (e.g. a
    # test fixture seeded directly) -- data_version is None, but the graph
    # itself is still built correctly from whatever's on disk.
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert snapshot.data_version is None
    assert set(snapshot.graph.nodes) == {1}


def test_warnings_propagate_from_build_result(engine):
    with session_scope(engine) as session:
        for i in range(1, 5):
            session.add(Terminal(id=i, name=f"T{i}", is_commodity_trading=True))

    from backend.models.db import Distance

    with session_scope(engine) as session:
        for a in range(1, 5):
            for b in range(1, 5):
                if a != b:
                    session.add(
                        Distance(terminal_a_id=a, terminal_b_id=b, distance=10.0, fetched_at=_now())
                    )

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings(graph_edge_count_guardrail=5))

    assert len(snapshot.warnings) == 1
    assert cache.get_warnings() == snapshot.warnings
    # get_warnings() returns a copy, not the live list.
    cache.get_warnings().append("mutated externally")
    assert len(cache.get_warnings()) == 1


# --- atomic swap: a previously-held graph reference is unaffected by a later rebuild --


def test_previously_held_graph_reference_survives_a_later_rebuild(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))

    cache = GraphCache()
    cache.rebuild(engine=engine, settings=_settings())
    old_graph = cache.get_graph()
    assert set(old_graph.nodes) == {1}

    with session_scope(engine) as session:
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
    cache.rebuild(engine=engine, settings=_settings())

    # The old reference a caller already grabbed is untouched -- a
    # different object from the new one, still showing the old data.
    new_graph = cache.get_graph()
    assert new_graph is not old_graph
    assert set(old_graph.nodes) == {1}
    assert set(new_graph.nodes) == {1, 2}


# --- commodity display names + atomic snapshot read ---------------------------


def test_commodity_names_available_via_snapshot_and_getter(engine):
    with session_scope(engine) as session:
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert snapshot.commodity_names == {1: "Laranite"}
    assert cache.get_commodity_names() == {1: "Laranite"}
    assert cache.get_snapshot() is snapshot

    # get_commodity_names() returns a copy, not the live dict.
    cache.get_commodity_names()[1] = "mutated externally"
    assert cache.get_commodity_names() == {1: "Laranite"}


# --- buy_prices / sell_prices (Phase 2) ---------------------------------------


def test_buy_and_sell_prices_available_via_snapshot_and_getters(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))

    with session_scope(engine) as session:
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    cache = GraphCache()
    snapshot = cache.rebuild(engine=engine, settings=_settings())

    assert snapshot.buy_prices == {1: {1: 100.0}}
    assert snapshot.sell_prices == {2: {1: 200.0}}
    assert cache.get_buy_prices() == {1: {1: 100.0}}
    assert cache.get_sell_prices() == {2: {1: 200.0}}

    # get_buy_prices()/get_sell_prices() return a fresh outer dict, not the
    # live one -- mutating the returned copy never touches the snapshot.
    cache.get_buy_prices()[999] = {1: 1.0}
    assert 999 not in cache.get_buy_prices()


def test_buy_and_sell_prices_survive_a_later_rebuild_atomic_swap(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
        session.add(Commodity(id=2, wiki_uuid="u2", slug="agricium", name="Agricium"))

    with session_scope(engine) as session:
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )

    cache = GraphCache()
    cache.rebuild(engine=engine, settings=_settings())
    old_snapshot = cache.get_snapshot()
    assert old_snapshot.buy_prices == {1: {1: 100.0}}

    with session_scope(engine) as session:
        # A new price row (distinct commodity, no PK conflict with the one
        # already committed) -- the point is a *changed* snapshot, not
        # specifically an updated one; a real refresh's upsert semantics
        # are `backend/ingest/refresh.py`'s concern, not this cache's.
        session.add(
            Price(terminal_id=1, commodity_id=2, price_buy=999.0, price_sell=None, fetched_at=_now())
        )
    cache.rebuild(engine=engine, settings=_settings())

    # The previously-held snapshot's price indices are untouched by the
    # later rebuild -- same atomic-swap guarantee as the graph itself.
    assert old_snapshot.buy_prices == {1: {1: 100.0}}
    assert cache.get_snapshot().buy_prices == {1: {1: 100.0, 2: 999.0}}


# --- process-wide singleton accessor ------------------------------------------


def test_get_graph_cache_returns_same_instance_across_calls():
    assert get_graph_cache() is get_graph_cache()
