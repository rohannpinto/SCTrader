"""Integration tests for `backend/ingest/refresh.py`.

All tests run fully offline against a temp-file SQLite DB: `respx`
intercepts every outbound `httpx` call the same way `test_wiki_client.py`
and `test_uex_client.py` do, mocked against the *real*, unmodified fixtures
captured in Tasks 2/3 (`tests/fixtures/wiki_*.json`, `tests/fixtures/
uex_*.json`). The only exception is the `GET /commodities` list page, which
is a small synthetic 3-item page built from the three real detail fixtures'
own `uuid`/`slug`/`name` (mirroring how `test_wiki_client.py` already
synthesizes list pages 2-3 for its pagination test) -- only three commodity
*detail* fixtures were captured, not the full ~206, so the list response has
to be trimmed to match what this test suite can actually serve.

Expected counts below (terminals=47, commodities=3, prices=60,
distances=72) were derived once, directly from the real fixtures, by
replicating `_reconcile`'s exact algorithm outside the client under test
(see the task's implementer notes) -- not guessed. The `uex_terminals_
sample.json` fixture (9 real Nyx-system commodity terminals, collapsing
onto 7 orbits) references only 3 of its own ids (778, 802, 830) from the
three commodities' real price data; the other 38 distinct terminal ids
referenced by that price data become stub terminals, since this fixture is
a small excerpt of UEX's real 826-terminal list, not the full thing.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest
import respx

from backend.config import Settings
from backend.ingest.refresh import run_refresh
from backend.models.db import (
    Commodity,
    Distance,
    Price,
    RefreshRun,
    Terminal,
    create_db_engine,
    init_db,
    session_scope,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
WIKI_BASE = "https://api.star-citizen.wiki/api"
UEX_BASE = "https://api.uexcorp.uk/2.0"

# Expected outcome of a full refresh against the real fixtures -- see module
# docstring for how these were derived.
EXPECTED_TERMINALS = 47
EXPECTED_COMMODITIES = 3
EXPECTED_PRICES = 60
EXPECTED_DISTANCES = 72

STANTG_ID = 802  # Nyx, id_orbit 397
LEVSKI_ID = 778  # Nyx, id_orbit 325
STANTG_LEVSKI_DISTANCE = 13.0  # CLAUDE.md's join example, real fixture value

# 396 (763, 764) and 397 (802, 803) are the two orbits shared by more than
# one commodity-trading terminal in `uex_terminals_sample.json`.
SAME_ORBIT_PAIRS = [(763, 764), (802, 803)]


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _settings(**overrides) -> Settings:
    defaults: dict = dict(
        wiki_api_base_url=WIKI_BASE,
        uex_api_base_url=UEX_BASE,
        http_timeout_seconds=1.0,
        http_max_retries=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Same rationale as the client test suites: retries run instantly."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))
    return calls


@pytest.fixture
def engine(tmp_path):
    """A SQLite engine bound to a temp *file* DB, with tables created."""
    db_file = tmp_path / "refresh_test.db"
    eng = create_db_engine(str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


def _commodity_list_payload() -> dict:
    """A small, synthetic list page whose 3 items match the 3 real detail
    fixtures available (see module docstring)."""
    items = []
    for fixture_name in (
        "wiki_commodity_laranite.json",
        "wiki_commodity_agricium.json",
        "wiki_commodity_inert_materials_sparse.json",
    ):
        detail = _load_fixture(fixture_name)["data"]
        items.append(
            {"uuid": detail["uuid"], "slug": detail["slug"], "name": detail["name"], "key": None}
        )
    return {
        "data": items,
        "links": {"next": None},
        "meta": {"current_page": 1, "last_page": 1},
    }


def _mock_successful_refresh(*, laranite_payload: dict | None = None) -> None:
    """Registers respx routes for a fully successful refresh, real fixtures.

    Must be called inside an active `respx.mock` context. `laranite_payload`
    lets one test substitute a hand-edited copy of the real laranite fixture
    (see `test_missing_price_stays_none_not_coerced_to_zero`) while every
    other route still serves the pristine, real fixture.
    """
    respx.get(f"{UEX_BASE}/terminals").mock(
        return_value=httpx.Response(200, json=_load_fixture("uex_terminals_sample.json"))
    )
    respx.get(f"{WIKI_BASE}/commodities").mock(
        return_value=httpx.Response(200, json=_commodity_list_payload())
    )
    respx.get(f"{WIKI_BASE}/commodities/laranite").mock(
        return_value=httpx.Response(
            200, json=laranite_payload or _load_fixture("wiki_commodity_laranite.json")
        )
    )
    respx.get(f"{WIKI_BASE}/commodities/agricium").mock(
        return_value=httpx.Response(200, json=_load_fixture("wiki_commodity_agricium.json"))
    )
    respx.get(f"{WIKI_BASE}/commodities/inert-materials").mock(
        return_value=httpx.Response(
            200, json=_load_fixture("wiki_commodity_inert_materials_sparse.json")
        )
    )
    respx.get(f"{UEX_BASE}/star_systems").mock(
        return_value=httpx.Response(200, json=_load_fixture("uex_star_systems_sample.json"))
    )
    respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 55}).mock(
        return_value=httpx.Response(200, json=_load_fixture("uex_orbits_distances_nyx.json"))
    )
    respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 64}).mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
    )
    respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 68}).mock(
        return_value=httpx.Response(200, json=_load_fixture("uex_orbits_distances_stanton.json"))
    )


# --- full successful run ----------------------------------------------------


@respx.mock
def test_full_successful_refresh_counts_and_known_values(engine):
    _mock_successful_refresh()

    result = run_refresh(engine=engine, settings=_settings())

    assert result.status == "success"
    assert result.success is True
    assert result.terminals_count == EXPECTED_TERMINALS
    assert result.commodities_count == EXPECTED_COMMODITIES
    assert result.prices_count == EXPECTED_PRICES
    assert result.distances_count == EXPECTED_DISTANCES

    with session_scope(engine) as session:
        run = session.get(RefreshRun, result.run_id)
        assert run.status == "success"
        assert run.started_at is not None
        assert run.completed_at is not None
        assert run.error_message is None
        assert run.terminals_count == EXPECTED_TERMINALS
        assert run.commodities_count == EXPECTED_COMMODITIES
        assert run.prices_count == EXPECTED_PRICES
        assert run.distances_count == EXPECTED_DISTANCES

        assert session.query(Terminal).count() == EXPECTED_TERMINALS
        assert session.query(Commodity).count() == EXPECTED_COMMODITIES
        assert session.query(Price).count() == EXPECTED_PRICES
        assert session.query(Distance).count() == EXPECTED_DISTANCES

        # STANTG <-> LEVSKI: CLAUDE.md's join example, real fixture value.
        stantg = session.query(Terminal).filter_by(uex_terminal_id=STANTG_ID).one()
        levski = session.query(Terminal).filter_by(uex_terminal_id=LEVSKI_ID).one()
        assert stantg.name == "Admin - Stanton Gateway (Nyx)"
        assert stantg.id_orbit == 397
        assert levski.id_orbit == 325

        dist = (
            session.query(Distance)
            .filter_by(terminal_a_id=stantg.id, terminal_b_id=levski.id)
            .one()
        )
        assert dist.distance == STANTG_LEVSKI_DISTANCE

        # A known price from wiki_commodity_laranite.json, exact match.
        laranite = session.query(Commodity).filter_by(slug="laranite").one()
        price = (
            session.query(Price)
            .filter_by(terminal_id=stantg.id, commodity_id=laranite.id)
            .one()
        )
        assert price.price_buy == 0.0
        assert price.price_sell == 8500.0
        assert price.source_date_updated is not None


# --- same-orbit divide-by-zero floor -----------------------------------------


@respx.mock
def test_same_orbit_terminal_pairs_get_min_distance_floor(engine):
    floor_value = 2.5  # deliberately non-default, proves it's read from settings
    _mock_successful_refresh()

    result = run_refresh(engine=engine, settings=_settings(min_distance_floor=floor_value))
    assert result.status == "success"

    with session_scope(engine) as session:
        ext_to_internal = {t.uex_terminal_id: t.id for t in session.query(Terminal).all()}
        for a_ext, b_ext in SAME_ORBIT_PAIRS:
            for x_ext, y_ext in ((a_ext, b_ext), (b_ext, a_ext)):
                row = (
                    session.query(Distance)
                    .filter_by(
                        terminal_a_id=ext_to_internal[x_ext],
                        terminal_b_id=ext_to_internal[y_ext],
                    )
                    .one()
                )
                assert row.distance == floor_value


# --- missing vs. zero price ---------------------------------------------------


@respx.mock
def test_missing_price_stays_none_not_coerced_to_zero(engine):
    # Hand-edit an in-memory copy of the real laranite fixture: force one
    # entry's price_buy to JSON null (not empirically observed live, but the
    # DB layer must still never coerce it to 0.0 -- see wiki_client.py's
    # "Null vs. zero prices" docstring section).
    mutated = json.loads(json.dumps(_load_fixture("wiki_commodity_laranite.json")))
    first_entry = mutated["data"]["uex_prices"]["purchase"][0]
    assert first_entry["terminal_id"] == STANTG_ID
    first_entry["price_buy"] = None

    _mock_successful_refresh(laranite_payload=mutated)

    result = run_refresh(engine=engine, settings=_settings())
    assert result.status == "success"

    with session_scope(engine) as session:
        stantg = session.query(Terminal).filter_by(uex_terminal_id=STANTG_ID).one()
        laranite = session.query(Commodity).filter_by(slug="laranite").one()
        price = (
            session.query(Price)
            .filter_by(terminal_id=stantg.id, commodity_id=laranite.id)
            .one()
        )
        assert price.price_buy is None
        assert price.price_sell == 8500.0  # untouched field on the same row


# --- idempotency --------------------------------------------------------------


@respx.mock
def test_running_refresh_twice_is_idempotent(engine):
    _mock_successful_refresh()
    first = run_refresh(engine=engine, settings=_settings())
    assert first.status == "success"

    with session_scope(engine) as session:
        terminal_ids_after_first = {t.uex_terminal_id: t.id for t in session.query(Terminal).all()}
        commodity_ids_after_first = {c.wiki_uuid: c.id for c in session.query(Commodity).all()}

    second = run_refresh(engine=engine, settings=_settings())
    assert second.status == "success"

    # Same counts, not doubled.
    assert second.terminals_count == first.terminals_count == EXPECTED_TERMINALS
    assert second.commodities_count == first.commodities_count == EXPECTED_COMMODITIES
    assert second.prices_count == first.prices_count == EXPECTED_PRICES
    assert second.distances_count == first.distances_count == EXPECTED_DISTANCES

    with session_scope(engine) as session:
        assert session.query(Terminal).count() == EXPECTED_TERMINALS
        assert session.query(Commodity).count() == EXPECTED_COMMODITIES
        assert session.query(Price).count() == EXPECTED_PRICES
        assert session.query(Distance).count() == EXPECTED_DISTANCES

        terminal_ids_after_second = {t.uex_terminal_id: t.id for t in session.query(Terminal).all()}
        commodity_ids_after_second = {c.wiki_uuid: c.id for c in session.query(Commodity).all()}

    # Internal ids stable across refreshes -- upsert, not insert-duplicate.
    assert terminal_ids_after_second == terminal_ids_after_first
    assert commodity_ids_after_second == commodity_ids_after_first
    # A second RefreshRun row was recorded, distinct from the first.
    assert second.run_id != first.run_id


# --- wiki-terminal-not-in-UEX-list stub edge case ----------------------------


@respx.mock
def test_wiki_terminal_missing_from_uex_creates_stub_and_preserves_price(engine, caplog):
    # terminal 511 ("Admin - Gaslight", Pyro) is real price data from
    # wiki_commodity_laranite.json but is NOT present in
    # uex_terminals_sample.json (a 9-terminal Nyx-only excerpt) -- exercises
    # the stub-terminal path without any hand-editing.
    stub_ext_id = 511

    with caplog.at_level(logging.WARNING, logger="backend.ingest.refresh"):
        _mock_successful_refresh()
        result = run_refresh(engine=engine, settings=_settings())

    assert result.status == "success"
    assert any(str(stub_ext_id) in message for message in caplog.messages)

    with session_scope(engine) as session:
        stub = session.query(Terminal).filter_by(uex_terminal_id=stub_ext_id).one()
        assert stub.wiki_terminal_id == stub_ext_id
        assert stub.name == "Admin - Gaslight"
        assert stub.code == "GASLI"
        assert stub.id_orbit is None
        assert stub.is_commodity_trading is True
        assert stub.star_system_name == "Pyro"

        laranite = session.query(Commodity).filter_by(slug="laranite").one()
        price = (
            session.query(Price)
            .filter_by(terminal_id=stub.id, commodity_id=laranite.id)
            .one()
        )
        assert price.price_sell == 8700.0

        # A stub terminal (unknown orbit) gets no Distance rows at all.
        assert session.query(Distance).filter_by(terminal_a_id=stub.id).count() == 0
        assert session.query(Distance).filter_by(terminal_b_id=stub.id).count() == 0


# --- failure handling: cache must be left completely untouched --------------


def test_failed_refresh_leaves_prior_data_untouched(engine):
    with respx.mock:
        _mock_successful_refresh()
        first = run_refresh(engine=engine, settings=_settings())
    assert first.status == "success"

    with session_scope(engine) as session:
        terminal_ids_before = {t.uex_terminal_id: t.id for t in session.query(Terminal).all()}
        commodity_ids_before = {c.wiki_uuid: c.id for c in session.query(Commodity).all()}
        prices_before = session.query(Price).count()
        distances_before = session.query(Distance).count()
        refresh_runs_before = session.query(RefreshRun).count()

    # A brand new respx context: only /terminals is mocked, and it always
    # 500s. If the pipeline incorrectly proceeded past the terminals fetch
    # anyway, the unmocked wiki/orbits_distances calls would raise a loud
    # respx "not mocked" error rather than silently succeeding.
    with respx.mock:
        respx.get(f"{UEX_BASE}/terminals").mock(
            return_value=httpx.Response(
                500, json={"status": "error", "http_code": 500, "message": "boom"}
            )
        )
        second = run_refresh(engine=engine, settings=_settings(http_max_retries=1))

    assert second.status == "failed"
    assert second.success is False
    assert second.error_message
    assert second.terminals_count is None
    assert second.commodities_count is None
    assert second.prices_count is None
    assert second.distances_count is None

    with session_scope(engine) as session:
        run = session.get(RefreshRun, second.run_id)
        assert run.status == "failed"
        assert run.completed_at is not None
        assert run.error_message

        # Every previously-cached row is untouched: same counts, same
        # internal ids -- not rolled back to empty, not partially rewritten.
        assert session.query(RefreshRun).count() == refresh_runs_before + 1
        assert {t.uex_terminal_id: t.id for t in session.query(Terminal).all()} == terminal_ids_before
        assert {c.wiki_uuid: c.id for c in session.query(Commodity).all()} == commodity_ids_before
        assert session.query(Price).count() == prices_before
        assert session.query(Distance).count() == distances_before
