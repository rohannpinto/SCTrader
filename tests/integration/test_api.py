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
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import backend.graph.cache as cache_module
import backend.models.db as db_module
from backend.graph.cache import get_graph_cache
from backend.models.db import Commodity, Distance, Price, Ship, Terminal, session_scope
from backend.rate_limit import limiter
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
