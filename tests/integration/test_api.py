"""Integration tests for the FastAPI app (`backend/main.py` + `backend/
routers/*.py`) via `fastapi.testclient.TestClient`.

Isolation strategy
----------------------
`backend.models.db` and `backend.graph.cache` hold process-wide singletons
(`_engine`/`_session_factory`, `_graph_cache`) that routers call through
`get_engine()`/`get_graph_cache()` rather than receiving via FastAPI
dependency injection -- so per-test isolation is done by monkeypatching
those module globals directly (a temp-file engine per test, a fresh
`GraphCache` per test), rather than `app.dependency_overrides`. `backend.
rate_limit.limiter` is also a process-wide singleton with its own request
counters that would otherwise leak between tests; `limiter.reset()` runs
autouse before every test.

`TestClient(app)` used as a context manager triggers `backend.main`'s
`lifespan` (startup: `init_db()` + a graph pre-warm `rebuild()`) against
whichever engine is currently monkeypatched in, so each test starts from a
freshly-initialized, empty temp DB and an empty graph.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import backend.graph.cache as cache_module
import backend.models.db as db_module
from backend.graph.cache import get_graph_cache
from backend.models.db import Commodity, Distance, Price, RefreshRun, Ship, Terminal, session_scope
from backend.rate_limit import limiter
from backend.routers import refresh as refresh_router
from backend.routers.route import _cached_search

WIKI_BASE = "https://api.star-citizen.wiki/api"
UEX_BASE = "https://api.uexcorp.uk/2.0"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_process_wide_singletons():
    """Runs before *every* test in this module -- see module docstring."""
    limiter.reset()
    _cached_search.cache_clear()
    yield


@pytest.fixture
def engine(tmp_path, monkeypatch):
    db_file = tmp_path / "api_test.db"
    eng = db_module.create_db_engine(str(db_file))
    monkeypatch.setattr(db_module, "_engine", eng)
    monkeypatch.setattr(db_module, "_session_factory", None)
    monkeypatch.setattr(cache_module, "_graph_cache", None)
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine):
    from backend.main import app

    # `raise_server_exceptions=False` matches real ASGI-server behavior
    # (uvicorn never re-raises into the caller) -- with the default `True`,
    # TestClient re-raises an unhandled exception into the test process
    # instead of letting `unhandled_exception_handler` convert it to a 500
    # response, which would make that handler untestable here.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --- health -------------------------------------------------------------------


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- /terminals -----------------------------------------------------------------


def test_terminals_empty_before_any_data(client):
    response = client.get("/terminals")
    assert response.status_code == 200
    assert response.json() == []


def test_terminals_reflects_graph_cache_after_manual_rebuild(client, engine):
    with session_scope(engine) as session:
        session.add(
            Terminal(
                id=1,
                name="Stanton Gateway",
                star_system_name="Nyx",
                location_name="Levski",
                is_commodity_trading=True,
                planet_name="Hurston",
                moon_name=None,
                is_orbital_station=True,
            )
        )

    get_graph_cache().rebuild(engine=engine)

    response = client.get("/terminals")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0] == {
        "id": 1,
        "name": "Stanton Gateway",
        "star_system_name": "Nyx",
        "location_name": "Levski",
        "planet_name": "Hurston",
        "moon_name": None,
        "is_orbital_station": True,
    }


# --- /ships (Task 11) -------------------------------------------------------------


def test_ships_empty_before_any_data(client):
    response = client.get("/ships")
    assert response.status_code == 200
    assert response.json() == []


def test_ships_returns_real_seeded_ships(client, engine):
    with session_scope(engine) as session:
        session.add(
            Ship(
                id=1,
                wiki_uuid="uuid-caterpillar",
                name="Caterpillar",
                manufacturer_name="Drake Interplanetary",
                quantum_range_gm=70.284406669,
                cargo_capacity_scu=576.0,
            )
        )
        session.add(
            Ship(
                id=2,
                wiki_uuid="uuid-avenger-stalker",
                name="Avenger Stalker",
                manufacturer_name="Aegis Dynamics",
                quantum_range_gm=112.244897959,
                cargo_capacity_scu=0.0,
            )
        )

    response = client.get("/ships")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    # Sorted by name -- "Avenger Stalker" before "Caterpillar".
    assert [ship["name"] for ship in body] == ["Avenger Stalker", "Caterpillar"]
    assert body[0] == {
        "id": 2,
        "name": "Avenger Stalker",
        "manufacturer_name": "Aegis Dynamics",
        "quantum_range_gm": 112.244897959,
        "cargo_capacity_scu": 0.0,
    }


def test_ships_reflects_null_manufacturer(client, engine):
    with session_scope(engine) as session:
        session.add(
            Ship(
                id=1,
                wiki_uuid="uuid-x",
                name="Mystery Ship",
                manufacturer_name=None,
                quantum_range_gm=50.0,
                cargo_capacity_scu=10.0,
            )
        )

    response = client.get("/ships")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["manufacturer_name"] is None


# --- /route: validation ----------------------------------------------------------


def _seed_ship(engine, **overrides) -> None:
    defaults = dict(
        id=1,
        wiki_uuid="uuid-test-ship",
        name="Test Ship",
        manufacturer_name="Test Manufacturer",
        quantum_range_gm=1000.0,
        cargo_capacity_scu=100.0,
    )
    defaults.update(overrides)
    with session_scope(engine) as session:
        session.add(Ship(**defaults))


def _valid_route_payload(**overrides) -> dict:
    payload = {"start_terminal_id": 1, "ship_id": 1, "num_hops": 5, "starting_budget": 1000.0}
    payload.update(overrides)
    return payload


def test_route_unknown_start_terminal_returns_404(client):
    # No terminal seeded at all -- the graph cache pre-warms empty, so
    # start_terminal_id=999 is unknown regardless of ship_id/num_hops/
    # starting_budget (which are otherwise schema-valid).
    response = client.post("/route", json=_valid_route_payload(start_terminal_id=999))
    assert response.status_code == 404


def test_route_unknown_ship_returns_404(client, engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    get_graph_cache().rebuild(engine=engine)

    # No Ship row seeded at all -- ship_id=1 must 404, same pattern as an
    # unknown start_terminal_id (CLAUDE.md security ground rules).
    response = client.post("/route", json=_valid_route_payload())
    assert response.status_code == 404


def test_route_num_hops_exceeding_cap_returns_422(client, engine):
    from backend.config import get_settings

    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    _seed_ship(engine)
    get_graph_cache().rebuild(engine=engine)

    response = client.post(
        "/route",
        json=_valid_route_payload(num_hops=get_settings().max_hops_cap + 1),
    )
    assert response.status_code == 422


def test_route_starting_budget_exceeding_cap_returns_422(client, engine):
    from backend.config import get_settings

    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    _seed_ship(engine)
    get_graph_cache().rebuild(engine=engine)

    response = client.post(
        "/route",
        json=_valid_route_payload(starting_budget=get_settings().max_starting_budget_cap + 1.0),
    )
    assert response.status_code == 422


def test_route_non_positive_num_hops_rejected_by_schema(client):
    response = client.post("/route", json=_valid_route_payload(num_hops=0))
    assert response.status_code == 422

    response = client.post("/route", json=_valid_route_payload(num_hops=-3))
    assert response.status_code == 422


def test_route_negative_starting_budget_rejected_by_schema(client):
    response = client.post("/route", json=_valid_route_payload(starting_budget=-1.0))
    assert response.status_code == 422


# --- /route: adversarial/malformed input never 500s --------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_terminal_id": "not-an-int"},
        {"ship_id": "not-an-int"},
        {"ship_id": None},
        {"num_hops": "not-an-int"},
        {"num_hops": 5.5},
        {"num_hops": -1_000_000},
        {"starting_budget": "not-a-float"},
        {"starting_budget": -1e30},
        {"num_hops": 10**18},
        {"starting_budget": 1e30},
    ],
)
def test_route_malformed_or_adversarial_input_returns_clean_4xx_not_500(client, overrides):
    response = client.post("/route", json=_valid_route_payload(**overrides))
    assert 400 <= response.status_code < 500


@pytest.mark.parametrize("field", ["start_terminal_id", "ship_id", "num_hops", "starting_budget"])
def test_route_missing_required_field_returns_422(client, field):
    payload = _valid_route_payload()
    del payload[field]
    response = client.post("/route", json=payload)
    assert response.status_code == 422


# --- /route: non-finite float (NaN/Infinity) returns clean 422, not 500 ------------
#
# Regression test for a gap found and deliberately deferred during Task 15's
# review (Task 17 fixes it): a raw JSON body carrying a non-finite float
# (`NaN`/`Infinity`/`-Infinity`) for a Pydantic-constrained numeric field is
# correctly rejected by Pydantic, but FastAPI's *default* RequestValidationError
# handler tries to echo the rejected value back into the 422 response body, and
# Starlette's `JSONResponse` (`allow_nan=False`) raises `ValueError` serializing
# it -- caught by the generic exception handler and turned into a `500`. See
# `backend/main.py`'s `validation_exception_handler` for the fix (a custom
# `RequestValidationError` handler that never echoes the raw rejected value).
#
# Python's own `json.dumps`/most JSON clients refuse to emit non-finite floats
# by default, so these use `content=` with a hand-written raw body containing
# the literal `NaN`/`Infinity`/`-Infinity` token, exactly like a client that
# isn't Python's stdlib `json` module could send.


@pytest.mark.parametrize(
    "raw_body,expected_missing_token",
    [
        # starting_budget: float field, `ge=0` -- NaN.
        (
            b'{"start_terminal_id": 1, "ship_id": 1, "num_hops": 5, "starting_budget": NaN}',
            "NaN",
        ),
        # starting_budget: float field, `ge=0` -- -Infinity.
        (
            b'{"start_terminal_id": 1, "ship_id": 1, "num_hops": 5, "starting_budget": -Infinity}',
            "Infinity",
        ),
        # num_hops: int field, `gt=0` -- Infinity (a different endpoint field,
        # confirming this isn't specific to `starting_budget` alone).
        (
            b'{"start_terminal_id": 1, "ship_id": 1, "num_hops": Infinity, "starting_budget": 1000}',
            "Infinity",
        ),
    ],
)
def test_route_non_finite_float_returns_422_not_500(client, raw_body, expected_missing_token):
    response = client.post(
        "/route", content=raw_body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert expected_missing_token not in response.text


# --- /route: real outcomes --------------------------------------------------------


def _seed_isolated_terminal(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Lonely Outpost", is_commodity_trading=True))


def test_route_isolated_terminal_returns_found_false(client, engine):
    _seed_isolated_terminal(engine)
    _seed_ship(engine)
    get_graph_cache().rebuild(engine=engine)

    response = client.post("/route", json=_valid_route_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["hops"] == []
    assert body["total_distance"] == 0.0
    assert body["total_profit"] == 0.0
    assert body["starting_budget"] == 1000.0
    assert body["final_cash"] == 1000.0
    assert body["message"]


def test_route_profitable_edge_returns_found_true_with_resolved_names(client, engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    _seed_ship(engine, quantum_range_gm=1000.0, cargo_capacity_scu=100.0)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    # cash=1000, buy=100 -> qty=min(floor(1000/100)=10, cargo=100)=10
    # profit = 10 * (200-100) = 1000 -> final_cash = 2000
    response = client.post("/route", json=_valid_route_payload(num_hops=1, starting_budget=1000.0))
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["start_terminal_id"] == 1
    assert body["total_distance"] == 10.0
    assert body["starting_budget"] == 1000.0
    assert body["final_cash"] == 2000.0
    assert body["total_profit"] == 1000.0
    assert body["hops"] == [
        {
            "terminal_id": 2,
            "terminal_name": "B",
            "commodity_id": 1,
            "commodity_name": "Laranite",
            "distance_from_previous": 10.0,
            "quantity_traded": 10.0,
            "unit_buy_price": 100.0,
            "unit_sell_price": 200.0,
            "profit_this_hop": 1000.0,
        }
    ]


def test_route_ship_jump_range_filters_out_too_far_edge(client, engine):
    # Same profitable edge as above, but the selected ship's quantum range
    # (5.0 Gm) is shorter than the edge's distance (10.0 Gm) -- the hop must
    # never be taken, so no route is found.
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    _seed_ship(engine, quantum_range_gm=5.0, cargo_capacity_scu=100.0)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    response = client.post("/route", json=_valid_route_payload(num_hops=1, starting_budget=1000.0))
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["final_cash"] == 1000.0


def test_route_multi_hop_realistic_search_produces_sane_final_cash(client, engine):
    # Chain A -> B -> C, both hops profitable via the same commodity, cargo
    # capped at 5 SCU so the arithmetic is easy to hand-verify (same shape
    # as tests/unit/test_search.py::test_num_hops_bounds_route_length):
    #   hop1 (A->B): cash=100 -> qty=min(floor(100/10)=10, 5)=5 -> profit 50 -> cash=150
    #   hop2 (B->C): cash=150 -> qty=min(floor(150/10)=15, 5)=5 -> profit 50 -> cash=200
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Terminal(id=3, name="C", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    _seed_ship(engine, quantum_range_gm=50.0, cargo_capacity_scu=5.0)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(Distance(terminal_a_id=2, terminal_b_id=3, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=10.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=10.0, price_sell=20.0, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=3, commodity_id=1, price_buy=None, price_sell=20.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    response = client.post(
        "/route", json=_valid_route_payload(num_hops=2, starting_budget=100.0)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["start_terminal_id"] == 1
    assert len(body["hops"]) == 2
    assert [hop["terminal_id"] for hop in body["hops"]] == [2, 3]
    assert body["total_distance"] == 20.0
    assert body["starting_budget"] == 100.0
    assert body["final_cash"] == 200.0
    assert body["total_profit"] == 100.0
    for hop in body["hops"]:
        assert hop["quantity_traded"] == 5.0
        assert hop["profit_this_hop"] == 50.0


# --- /route: risk_level -> rank mapping (Phase 3, Task 21) --------------------------


def _seed_multi_route_scenario(engine):
    """Terminals 1 (start) -> {2, 3, 5} -> {4}, mirroring tests/unit/
    test_search.py::test_three_plus_distinct_routes_ranked_by_final_cash_exactly
    exactly (renumbered so terminal ids start at 1: old node 3 -- the
    hop-2 convergence node -- becomes new terminal 4; old node 5 stays 5).
    Five distinct profitable routes from terminal 1, hand-verified final
    cash (starting_budget=1000.0, ship cargo/jump-range huge so only cash
    gates quantity and distance never filters an edge):
      rank 1: 10000.0, path [2, 4]
      rank 2: 6000.0,  path [3, 4]
      rank 3: 3000.0,  path [3]
      rank 4: 2500.0,  path [5]
      rank 5: 2000.0,  path [2]
    A requested rank > 5 clamps down to rank 5 (2000.0, path [2]).
    """
    with session_scope(engine) as session:
        for terminal_id, name in [
            (1, "Start"),
            (2, "Node2"),
            (3, "Node3"),
            (4, "Node4"),
            (5, "Node5"),
        ]:
            session.add(Terminal(id=terminal_id, name=name, is_commodity_trading=True))
        for commodity_id in (1, 2, 3, 4):
            session.add(
                Commodity(
                    id=commodity_id,
                    wiki_uuid=f"u{commodity_id}",
                    slug=f"c{commodity_id}",
                    name=f"Commodity {commodity_id}",
                )
            )

    _seed_ship(engine, quantum_range_gm=100.0, cargo_capacity_scu=10_000.0)

    with session_scope(engine) as session:
        for a, b in [(1, 2), (1, 3), (1, 5), (2, 4), (3, 4)]:
            session.add(Distance(terminal_a_id=a, terminal_b_id=b, distance=5.0, fetched_at=_now()))

        # Buy side.
        session.add(Price(terminal_id=1, commodity_id=1, price_buy=10.0, price_sell=None, fetched_at=_now()))
        session.add(Price(terminal_id=1, commodity_id=2, price_buy=5.0, price_sell=None, fetched_at=_now()))
        session.add(Price(terminal_id=1, commodity_id=3, price_buy=8.0, price_sell=None, fetched_at=_now()))
        session.add(Price(terminal_id=2, commodity_id=3, price_buy=5.0, price_sell=None, fetched_at=_now()))
        session.add(Price(terminal_id=3, commodity_id=4, price_buy=10.0, price_sell=None, fetched_at=_now()))

        # Sell side.
        session.add(Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=20.0, fetched_at=_now()))
        session.add(Price(terminal_id=3, commodity_id=2, price_buy=None, price_sell=15.0, fetched_at=_now()))
        session.add(Price(terminal_id=5, commodity_id=3, price_buy=None, price_sell=20.0, fetched_at=_now()))
        session.add(Price(terminal_id=4, commodity_id=3, price_buy=None, price_sell=25.0, fetched_at=_now()))
        session.add(Price(terminal_id=4, commodity_id=4, price_buy=None, price_sell=20.0, fetched_at=_now()))

    get_graph_cache().rebuild(engine=engine)


def test_route_risk_level_to_rank_mapping_is_exactly_max_1_10_minus_risk_level():
    from backend.routers.route import _rank_from_risk_level

    assert _rank_from_risk_level(10) == 1  # endpoint: max risk tolerance -> best route
    assert _rank_from_risk_level(0) == 10  # endpoint: risk-averse -> 10th-best route
    assert _rank_from_risk_level(4) == 6  # a middle value
    assert _rank_from_risk_level(9) == 1  # max(1, 1) == 1, not 0
    assert _rank_from_risk_level(1) == 9


def test_route_risk_level_10_and_0_return_genuinely_different_routes(client, engine):
    _seed_multi_route_scenario(engine)

    high_risk = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=10)).json()
    low_risk = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=0)).json()

    assert high_risk["found"] is True
    assert low_risk["found"] is True

    # risk_level=10 -> rank=1 -> the single best route.
    assert high_risk["requested_rank"] == 1
    assert high_risk["actual_rank_used"] == 1
    assert high_risk["final_cash"] == 10000.0
    assert [hop["terminal_id"] for hop in high_risk["hops"]] == [2, 4]

    # risk_level=0 -> rank=10 -> clamps to the 5th (least-profitable found).
    assert low_risk["requested_rank"] == 10
    assert low_risk["actual_rank_used"] == 5
    assert low_risk["final_cash"] == 2000.0
    assert [hop["terminal_id"] for hop in low_risk["hops"]] == [2]

    # Genuinely different results, not a coincidence of shared defaults.
    assert high_risk["hops"] != low_risk["hops"]
    assert high_risk["final_cash"] != low_risk["final_cash"]

    # Higher risk_level (better-or-equal rank) never does worse in cash.
    assert high_risk["final_cash"] >= low_risk["final_cash"]


def test_route_risk_level_middle_value_returns_correct_rank(client, engine):
    _seed_multi_route_scenario(engine)

    response = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=8))
    assert response.status_code == 200
    body = response.json()
    # risk_level=8 -> rank=max(1, 10-8)=2 -> the second-best route.
    assert body["requested_rank"] == 2
    assert body["actual_rank_used"] == 2
    assert body["final_cash"] == 6000.0
    assert [hop["terminal_id"] for hop in body["hops"]] == [3, 4]


# --- /route: cache key correctness -- risk_level must not collide -------------------


def test_route_cache_does_not_collide_across_different_risk_levels(client, engine):
    """The real, convincing version of the cache-key test: issue risk_level=10
    then risk_level=0 (known, hand-verified to produce DIFFERENT routes on
    this seeded graph -- see `_seed_multi_route_scenario`'s docstring), then
    re-issue risk_level=10 again and confirm it still returns risk_level=10's
    original answer, not risk_level=0's. If the cache key omitted `rank`
    (or `risk_level`), this second risk_level=10 request would collide on
    the exact same key the risk_level=0 request just overwrote with a
    different result -- this test would fail loudly if that regressed.
    """
    _seed_multi_route_scenario(engine)

    first_high = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=10)).json()
    low = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=0)).json()
    second_high = client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=10)).json()

    assert first_high["final_cash"] == 10000.0
    assert low["final_cash"] == 2000.0
    # If risk_level weren't part of the cache key, this would incorrectly
    # come back as 2000.0 (the low-risk request's cached entry, colliding
    # on a key identical in every other component).
    assert second_high == first_high
    assert second_high["final_cash"] == 10000.0
    assert second_high["final_cash"] != low["final_cash"]


def test_route_cache_holds_distinct_entries_per_rank(client, engine):
    """Direct inspection of the hand-rolled LRU: after two requests
    differing only in `risk_level` (-> different `rank`), there must be two
    distinct cache entries, not one overwriting the other."""
    from backend.routers.route import _route_cache

    _seed_multi_route_scenario(engine)

    client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=10))
    client.post("/route", json=_valid_route_payload(num_hops=2, risk_level=0))

    # Keys are (id(graph), start_terminal_id, ship_id, num_hops,
    # starting_budget, rank, data_version) -- filter to this scenario's
    # start_terminal_id=1/ship_id=1/num_hops=2/starting_budget=1000.0 and
    # confirm both rank=1 (risk_level=10) and rank=10 (risk_level=0) are
    # present as separate entries with different cached results.
    matching = [
        (key, result)
        for key, result in _route_cache.items()
        if key[1] == 1 and key[2] == 1 and key[3] == 2 and key[4] == 1000.0
    ]
    ranks_present = {key[5] for key, _ in matching}
    assert {1, 10} <= ranks_present

    by_rank = {key[5]: result for key, result in matching}
    assert by_rank[1].final_cash != by_rank[10].final_cash


# --- /route: clamped rank visibility (Task 20's clamp, surfaced at the API) ---------


def test_route_response_surfaces_clamped_rank_when_insufficient_routes_exist(client, engine):
    """Task 20's clamp scenario, confirmed at the API layer: seed a graph
    with only ONE distinct profitable route, request a low risk_level
    (-> a high rank) that asks for more distinct routes than exist, and
    confirm the response's `actual_rank_used` reflects the clamp while
    `requested_rank` still reflects what was actually asked for."""
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    _seed_ship(engine, quantum_range_gm=1000.0, cargo_capacity_scu=100.0)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    # risk_level=0 -> rank=10, but only 1 distinct profitable route exists.
    response = client.post(
        "/route", json=_valid_route_payload(num_hops=1, starting_budget=1000.0, risk_level=0)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["requested_rank"] == 10
    assert body["actual_rank_used"] == 1
    assert body["final_cash"] == 2000.0


# --- /route: risk_level adversarial/malformed input never 500s ---------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_level": "not-an-int"},
        {"risk_level": -1},
        {"risk_level": 11},
        {"risk_level": 5.5},
        {"risk_level": None},
        {"risk_level": 10**9},
    ],
)
def test_route_risk_level_adversarial_input_returns_clean_4xx_not_500(client, overrides):
    response = client.post("/route", json=_valid_route_payload(**overrides))
    assert 400 <= response.status_code < 500


def test_route_missing_risk_level_defaults_to_10_and_matches_pre_phase_3_behavior(client, engine):
    """`risk_level` is optional with a default of `10` specifically so a
    caller that never sends it (every existing test in this file above this
    section, and every pre-Phase-3 client) keeps getting `rank=1` -- today's
    exact single-best-route behavior, unchanged."""
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))
    _seed_ship(engine, quantum_range_gm=1000.0, cargo_capacity_scu=100.0)

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    payload = _valid_route_payload(num_hops=1, starting_budget=1000.0)
    assert "risk_level" not in payload
    response = client.post("/route", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["requested_rank"] == 1
    assert body["actual_rank_used"] == 1
    assert body["final_cash"] == 2000.0


# --- /refresh-status --------------------------------------------------------------


def test_refresh_status_never_run_before_any_refresh(client):
    response = client.get("/refresh-status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "never_run"
    assert body["data_version"] is None


# --- /refresh: shared-secret gate --------------------------------------------------


def test_refresh_token_disabled_by_default(client, monkeypatch):
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_token", None)
    with respx.mock:
        respx.get(f"{UEX_BASE}/terminals").mock(
            return_value=httpx.Response(500, json={"status": "error", "message": "boom"})
        )
        # No token header supplied at all -- gate disabled means this must
        # reach run_refresh() (and fail there, gracefully, not with a 401).
        response = client.post("/refresh")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_refresh_rejects_missing_token_when_configured(client, monkeypatch):
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_token", "expected-secret")
    response = client.post("/refresh")
    assert response.status_code == 401


def test_refresh_rejects_wrong_token_when_configured(client, monkeypatch):
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_token", "expected-secret")
    response = client.post("/refresh", headers={"X-Refresh-Token": "wrong"})
    assert response.status_code == 401


def test_refresh_accepts_correct_token_when_configured(client, monkeypatch):
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_token", "expected-secret")
    with respx.mock:
        respx.get(f"{UEX_BASE}/terminals").mock(
            return_value=httpx.Response(500, json={"status": "error", "message": "boom"})
        )
        response = client.post("/refresh", headers={"X-Refresh-Token": "expected-secret"})
    # Gets past the token gate; fails later for an unrelated reason (no full
    # mock set up), but must not be a 401.
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


# --- /refresh: overlap guard --------------------------------------------------------


def test_refresh_overlap_guard_returns_409(client):
    from backend.routers.refresh import _refresh_lock

    assert _refresh_lock.acquire(blocking=False)
    try:
        response = client.post("/refresh")
        assert response.status_code == 409
    finally:
        _refresh_lock.release()


# --- /refresh: full success path rebuilds the graph cache ----------------------------


def test_refresh_success_updates_status_and_graph_cache(client):
    with respx.mock:
        respx.get(f"{UEX_BASE}/terminals").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": [
                        {
                            "id": 1,
                            "name": "Terminal A",
                            "code": "TERMA",
                            "type": "commodity",
                            "id_star_system": 55,
                            "id_orbit": 100,
                            "star_system_name": "Nyx",
                        }
                    ],
                    "message": "",
                },
            )
        )
        respx.get(f"{WIKI_BASE}/commodities").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [],
                    "links": {"next": None},
                    "meta": {"current_page": 1, "last_page": 1},
                },
            )
        )
        respx.get(f"{UEX_BASE}/star_systems").mock(
            return_value=httpx.Response(200, json=_load_fixture("uex_star_systems_sample.json"))
        )
        respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 55}).mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
        )
        respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 64}).mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
        )
        respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": 68}).mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
        )
        respx.get(f"{WIKI_BASE}/vehicles").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [],
                    "links": {"next": None},
                    "meta": {"current_page": 1, "last_page": 1},
                },
            )
        )

        response = client.post("/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["terminals_count"] == 1
    assert body["commodities_count"] == 0
    assert body["ships_count"] == 0
    assert body["data_version"] is not None

    status_response = client.get("/refresh-status")
    assert status_response.json()["status"] == "success"

    terminals_response = client.get("/terminals")
    assert terminals_response.status_code == 200
    assert len(terminals_response.json()) == 1
    assert terminals_response.json()[0]["name"] == "Terminal A"


# --- generic error handler: never leak internal exception details --------------------


def test_unhandled_exception_returns_generic_500(client, monkeypatch):
    def _boom():
        raise RuntimeError("some sensitive internal detail that must never reach the client")

    monkeypatch.setattr("backend.routers.terminals.get_graph_cache", _boom)

    response = client.get("/terminals")
    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "An internal error occurred."}
    assert "sensitive internal detail" not in response.text


# --- rate limiting: /refresh (tightest configured limit) -----------------------------


def test_refresh_rate_limit_returns_429_once_exceeded(client, monkeypatch):
    from backend.config import get_settings

    # Bogus token -> every request 401s immediately, before touching the
    # network or the refresh lock -- but the rate limiter (outermost
    # decorator) still counts each one, so this exercises the real wiring
    # without needing respx mocks for 6 requests.
    monkeypatch.setattr(get_settings(), "refresh_token", "expected-secret")

    statuses = [client.post("/refresh").status_code for _ in range(6)]

    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


# --- shared refresh-trigger function: backend.routers.refresh.run_refresh_cycle -------
#
# Tested directly (no HTTP, no scheduler) for its three transport-agnostic
# outcomes -- Task 19. `trigger_refresh`'s existing tests above are left
# completely unmodified; their continued passing is what proves this
# extraction didn't change the HTTP-visible behavior of `/refresh`.


def _mock_minimal_successful_refresh() -> None:
    """Sets up the same minimal-but-successful respx mock set used by
    `test_refresh_success_updates_status_and_graph_cache` above, factored
    out so `run_refresh_cycle`'s own success test can reuse it without
    going through `client.post("/refresh")`.
    """
    respx.get(f"{UEX_BASE}/terminals").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": [
                    {
                        "id": 1,
                        "name": "Terminal A",
                        "code": "TERMA",
                        "type": "commodity",
                        "id_star_system": 55,
                        "id_orbit": 100,
                        "star_system_name": "Nyx",
                    }
                ],
                "message": "",
            },
        )
    )
    respx.get(f"{WIKI_BASE}/commodities").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "links": {"next": None}, "meta": {"current_page": 1, "last_page": 1}},
        )
    )
    respx.get(f"{UEX_BASE}/star_systems").mock(
        return_value=httpx.Response(200, json=_load_fixture("uex_star_systems_sample.json"))
    )
    for system_id in (55, 64, 68):
        respx.get(f"{UEX_BASE}/orbits_distances", params={"id_star_system": system_id}).mock(
            return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
        )
    respx.get(f"{WIKI_BASE}/vehicles").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "links": {"next": None}, "meta": {"current_page": 1, "last_page": 1}},
        )
    )


def test_run_refresh_cycle_ran_and_succeeded(client):
    from backend.config import get_settings

    with respx.mock:
        _mock_minimal_successful_refresh()
        result = refresh_router.run_refresh_cycle(get_settings())

    assert result.outcome is refresh_router.RefreshTriggerOutcome.RAN
    assert result.run is not None
    assert result.run.status == "success"
    assert result.snapshot is not None
    assert result.snapshot.data_version == result.run.id
    # The graph cache was actually rebuilt from the freshly-written data --
    # not just "run_refresh() succeeded" bookkeeping.
    assert get_graph_cache().get_snapshot().data_version == result.run.id


def test_run_refresh_cycle_ran_and_failed(client):
    from backend.config import get_settings

    with respx.mock:
        respx.get(f"{UEX_BASE}/terminals").mock(
            return_value=httpx.Response(500, json={"status": "error", "message": "boom"})
        )
        result = refresh_router.run_refresh_cycle(get_settings())

    assert result.outcome is refresh_router.RefreshTriggerOutcome.RAN
    assert result.run is not None
    assert result.run.status == "failed"
    assert result.snapshot is not None


def test_run_refresh_cycle_skipped_due_to_overlap(client):
    from backend.config import get_settings

    assert refresh_router._refresh_lock.acquire(blocking=False)
    try:
        result = refresh_router.run_refresh_cycle(get_settings())
    finally:
        refresh_router._refresh_lock.release()

    assert result.outcome is refresh_router.RefreshTriggerOutcome.SKIPPED_OVERLAP
    assert result.run is None
    assert result.snapshot is None


# --- auto-refresh scheduler: backend.main -----------------------------------------
#
# None of these wait out a real interval -- `_auto_refresh_tick` (one tick's
# worth of work) is tested directly wherever possible, and the two tests
# that do exercise the real background thread use either an
# already-`set()` stop event (interrupts an otherwise-huge wait instantly)
# or a zero-minute interval (times out instantly, with only a short bounded
# poll for thread scheduling -- never a sleep anywhere near real minutes).


def test_auto_refresh_scheduler_thread_starts_and_stops_cleanly_when_enabled(engine, monkeypatch):
    from backend.config import get_settings
    from backend.main import _SCHEDULER_THREAD_NAME, app

    monkeypatch.setattr(get_settings(), "auto_refresh_enabled", True)
    monkeypatch.setattr(get_settings(), "auto_refresh_interval_minutes", 60)

    with TestClient(app, raise_server_exceptions=False):
        assert _SCHEDULER_THREAD_NAME in {t.name for t in threading.enumerate()}

    # Shutdown (lifespan's stop_event.set() + bounded join()) must have
    # actually stopped the thread promptly, not merely returned control
    # while a 60-minute wait kept running in the background -- if the
    # stop-event mechanism didn't interrupt the wait, the thread would
    # still be alive and enumerable here.
    assert _SCHEDULER_THREAD_NAME not in {t.name for t in threading.enumerate()}


def test_auto_refresh_scheduler_disabled_starts_no_thread(engine, monkeypatch):
    from backend.config import get_settings
    from backend.main import _SCHEDULER_THREAD_NAME, app

    monkeypatch.setattr(get_settings(), "auto_refresh_enabled", False)

    with TestClient(app, raise_server_exceptions=False):
        assert _SCHEDULER_THREAD_NAME not in {t.name for t in threading.enumerate()}


def test_auto_refresh_tick_calls_shared_refresh_function(monkeypatch):
    import backend.main as main_module
    from backend.config import get_settings

    calls = []

    def fake_run_refresh_cycle(settings):
        calls.append(settings)
        return refresh_router.RefreshTriggerResult(
            outcome=refresh_router.RefreshTriggerOutcome.RAN, run=None, snapshot=None
        )

    monkeypatch.setattr(main_module, "run_refresh_cycle", fake_run_refresh_cycle)

    settings = get_settings()
    main_module._auto_refresh_tick(settings)

    assert calls == [settings]


def test_auto_refresh_tick_skipped_when_refresh_already_in_progress(client, monkeypatch):
    import backend.main as main_module
    from backend.config import get_settings

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("run_refresh() must not be called when _refresh_lock is already held")

    # `run_refresh_cycle` is exercised for real here (not mocked) -- only
    # the thing it would call *after* successfully acquiring the lock is
    # stubbed to blow up, so this proves the overlap guard itself (not a
    # mock) is what prevents the tick from proceeding.
    monkeypatch.setattr(refresh_router, "run_refresh", _fail_if_called)

    with session_scope() as session:
        before_count = session.query(RefreshRun).count()

    assert refresh_router._refresh_lock.acquire(blocking=False)
    try:
        main_module._auto_refresh_tick(get_settings())  # must not raise, must not call run_refresh
    finally:
        refresh_router._refresh_lock.release()

    with session_scope() as session:
        after_count = session.query(RefreshRun).count()
    assert after_count == before_count


def test_auto_refresh_loop_stop_event_interrupts_wait_immediately(monkeypatch):
    import backend.main as main_module
    from backend.config import get_settings

    # The largest interval Settings actually allows in production
    # (auto_refresh_interval_minutes' new le=4_320 bound, 3 days = 259,200
    # seconds) -- large enough that if the stop-event interrupt didn't
    # genuinely work, the thread would still be waiting when this test's
    # bounded join() times out, but nowhere near the ~4,294,967-second
    # Windows Event.wait() ceiling a much larger value used to hit (see
    # test_auto_refresh_loop_survives_interval_that_used_to_overflow below
    # for that specific regression). This version proves the interrupt
    # mechanism itself, not an accidental crash.
    monkeypatch.setattr(get_settings(), "auto_refresh_interval_minutes", 4_320)

    tick_calls = []
    monkeypatch.setattr(main_module, "_auto_refresh_tick", lambda settings: tick_calls.append(settings))

    stop_event = threading.Event()
    thread = threading.Thread(
        target=main_module._auto_refresh_loop,
        args=(get_settings(), stop_event),
        daemon=True,
    )
    start = time.monotonic()
    thread.start()
    stop_event.set()
    thread.join(timeout=5.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive()
    assert elapsed < 5.0
    assert tick_calls == []  # stopped before ever firing a tick


def test_auto_refresh_loop_survives_interval_that_used_to_overflow(monkeypatch):
    """Regression guard for the exact bug a reviewer caught: on this Windows
    Python build, `threading.Event.wait()`'s `timeout` is bounded by the
    underlying `WaitForSingleObject` API's DWORD-milliseconds ceiling
    (~4,294,967 seconds =~ 49.7 days). Before `_wait_for_interval_or_stop`'s
    chunking existed, a single `stop_event.wait(timeout=interval_seconds)`
    call for an interval this large raised an immediate, unhandled
    `OverflowError` that killed the scheduler thread silently -- the loop
    "exited" in ~0 seconds via a crash, not via a real interrupt (which is
    exactly why the old version of the test above, using this same
    magnitude of interval, was a false positive: it couldn't tell a crash
    from a working interrupt).

    This interval (999,999 minutes =~ 1.9 years) is far larger than
    `Settings.auto_refresh_interval_minutes`'s new `le=4_320` bound allows
    in production -- set here via `monkeypatch` exactly like every other
    test in this file sets `Settings` fields directly, deliberately
    bypassing that bound. The point of *this* test is to prove the
    chunked-wait mechanism itself is what makes an oversized interval safe
    now, independently of the config-level guard added alongside it.
    """
    import backend.main as main_module
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_refresh_interval_minutes", 999_999)
    # A tiny chunk size so several real chunk boundaries elapse within the
    # test's short run -- proves the loop actually keeps re-checking the
    # stop event between chunks, not merely that construction/the first
    # call avoids crashing.
    monkeypatch.setattr(main_module, "_MAX_SINGLE_WAIT_SECONDS", 0.05)

    tick_calls = []
    monkeypatch.setattr(main_module, "_auto_refresh_tick", lambda settings: tick_calls.append(settings))

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            main_module._auto_refresh_loop(get_settings(), stop_event)
        except BaseException as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    stop_event = threading.Event()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(0.2)  # let several 0.05s chunks genuinely elapse for real
    stop_event.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert errors == []  # no OverflowError (or anything else) escaped the loop
    assert tick_calls == []  # stopped long before the huge interval could fully elapse


def test_auto_refresh_loop_logs_and_continues_after_unexpected_exception(monkeypatch, caplog):
    """Defense-in-depth check: an exception from the loop machinery itself
    (not from `run_refresh_cycle`, which `_auto_refresh_tick` already
    handles on its own) must be logged and must not kill the thread --
    `backend/logging_config.py` never hooks `threading.excepthook`, so an
    uncaught exception here would otherwise vanish with no trace at all.
    """
    import logging as logging_module

    import backend.main as main_module
    from backend.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_refresh_interval_minutes", 60)

    real_wait = main_module._wait_for_interval_or_stop
    call_count = {"n": 0}

    def _wait_raise_once_then_real(interval_seconds, stop_event):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated unexpected bug in the wait helper")
        return real_wait(interval_seconds, stop_event)

    tick_calls = []
    monkeypatch.setattr(main_module, "_wait_for_interval_or_stop", _wait_raise_once_then_real)
    monkeypatch.setattr(main_module, "_auto_refresh_tick", lambda settings: tick_calls.append(settings))

    stop_event = threading.Event()
    thread = threading.Thread(
        target=main_module._auto_refresh_loop,
        args=(get_settings(), stop_event),
        daemon=True,
    )

    with caplog.at_level(logging_module.ERROR, logger="backend.main"):
        thread.start()
        deadline = time.monotonic() + 2.0
        while call_count["n"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop_event.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert call_count["n"] >= 2  # the loop retried after the exception instead of dying
    assert tick_calls == []  # never reached a tick in this test
    assert "unexpected exception" in caplog.text.lower()


def test_auto_refresh_loop_fires_tick_then_stops_cleanly(monkeypatch):
    import backend.main as main_module
    from backend.config import get_settings

    # 0 minutes -> Event.wait(timeout=0) always times out instantly, so the
    # loop fires ticks back-to-back without waiting out any real interval.
    monkeypatch.setattr(get_settings(), "auto_refresh_interval_minutes", 0)

    tick_calls = []
    monkeypatch.setattr(main_module, "_auto_refresh_tick", lambda settings: tick_calls.append(settings))

    stop_event = threading.Event()
    thread = threading.Thread(
        target=main_module._auto_refresh_loop,
        args=(get_settings(), stop_event),
        daemon=True,
    )
    thread.start()

    # Bounded poll purely for thread scheduling, never for the interval
    # itself (interval is 0) -- caps out at 2s worst case.
    deadline = time.monotonic() + 2.0
    while not tick_calls and time.monotonic() < deadline:
        time.sleep(0.01)

    stop_event.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert len(tick_calls) >= 1
