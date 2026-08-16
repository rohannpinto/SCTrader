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


def test_route_unknown_start_terminal_returns_404(client):
    response = client.post("/route", json={"start_terminal_id": 999, "max_distance": 100.0})
    assert response.status_code == 404


def test_route_max_distance_exceeding_cap_returns_422(client, engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    get_graph_cache().rebuild(engine=engine)

    response = client.post(
        "/route", json={"start_terminal_id": 1, "max_distance": 10_000_000.0}
    )
    assert response.status_code == 422


def test_route_distance_threshold_exceeding_cap_returns_422(client, engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
    get_graph_cache().rebuild(engine=engine)

    response = client.post(
        "/route",
        json={"start_terminal_id": 1, "max_distance": 100.0, "distance_threshold": 10_000_000.0},
    )
    assert response.status_code == 422


def test_route_non_positive_max_distance_rejected_by_schema(client):
    response = client.post("/route", json={"start_terminal_id": 1, "max_distance": 0.0})
    assert response.status_code == 422


# --- /route: real outcomes --------------------------------------------------------


def _seed_isolated_terminal(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Lonely Outpost", is_commodity_trading=True))


def test_route_isolated_terminal_returns_found_false(client, engine):
    _seed_isolated_terminal(engine)
    get_graph_cache().rebuild(engine=engine)

    response = client.post("/route", json={"start_terminal_id": 1, "max_distance": 100.0})
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["hops"] == []
    assert body["total_distance"] == 0.0
    assert body["total_profit"] == 0.0
    assert body["message"]


def test_route_profitable_edge_returns_found_true_with_resolved_names(client, engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="A", is_commodity_trading=True))
        session.add(Terminal(id=2, name="B", is_commodity_trading=True))
        session.add(Commodity(id=1, wiki_uuid="u1", slug="laranite", name="Laranite"))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=10.0, fetched_at=_now()))
        session.add(
            Price(terminal_id=1, commodity_id=1, price_buy=100.0, price_sell=None, fetched_at=_now())
        )
        session.add(
            Price(terminal_id=2, commodity_id=1, price_buy=None, price_sell=200.0, fetched_at=_now())
        )

    get_graph_cache().rebuild(engine=engine)

    response = client.post("/route", json={"start_terminal_id": 1, "max_distance": 100.0})
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["start_terminal_id"] == 1
    assert body["total_distance"] == 10.0
    assert body["total_profit"] == 100.0
    assert body["hops"] == [
        {
            "terminal_id": 2,
            "terminal_name": "B",
            "commodity_id": 1,
            "commodity_name": "Laranite",
            "distance_from_previous": 10.0,
            "profit_this_hop": 100.0,
        }
    ]


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
