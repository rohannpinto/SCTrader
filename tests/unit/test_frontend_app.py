"""Lightweight smoke tests for `frontend/app.py` via Streamlit's
`AppTest` framework, with backend HTTP calls intercepted by `respx` (same
mocking approach used everywhere else in this test suite) rather than a
real running backend.

These are deliberately shallow: `AppTest` re-executes the script's actual
source each `.run()`, so this exercises real rendering logic, but the goal
here is basic "doesn't crash, shows the right top-level state" coverage,
not a full behavioral spec of every widget interaction.

Phase 2 (Task 16): `main()` now always fetches both `/terminals` and
`/ships` (the route form needs a ship selection), so every test below
mocks both endpoints even when a test cares only about one of them.
"""

from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest
import respx
import streamlit as st
from streamlit.proto import SelectWidgetFilterMode_pb2
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parent.parent.parent / "frontend" / "app.py")
BACKEND_BASE_URL = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """`@st.cache_data` on `_fetch_terminals()`/`_fetch_ships()` is a
    process-wide cache keyed by function+args, with no per-`AppTest`-run
    isolation -- without clearing it, whichever test runs first "wins" the
    cache entry and every later test silently reuses that stale result
    instead of hitting its own respx mock.
    """
    st.cache_data.clear()
    yield


def _find_button(at: AppTest, label: str):
    """`at.button` includes every button on the page (sidebar's "Refresh
    data now" is declared before the main content's "Find best route"), so
    tests select by label rather than assuming a position.
    """
    matches = [button for button in at.button if button.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} button, found {len(matches)}"
    return matches[0]


def _find_selectbox(at: AppTest, label: str):
    matches = [box for box in at.selectbox if box.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} selectbox, found {len(matches)}"
    return matches[0]


def _find_number_input(at: AppTest, label: str):
    matches = [box for box in at.number_input if box.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} number_input, found {len(matches)}"
    return matches[0]


def _find_checkbox(at: AppTest, label: str):
    matches = [box for box in at.checkbox if box.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} checkbox, found {len(matches)}"
    return matches[0]


def _never_run_status() -> dict:
    return {
        "status": "never_run",
        "started_at": None,
        "completed_at": None,
        "commodities_count": None,
        "terminals_count": None,
        "prices_count": None,
        "distances_count": None,
        "error_message": None,
        "data_version": None,
        "warnings": [],
    }


def _ships_payload() -> list[dict]:
    return [
        {
            "id": 1,
            "name": "Freelancer",
            "manufacturer_name": "MISC",
            "quantum_range_gm": 100.0,
            "cargo_capacity_scu": 66.0,
        },
        {
            "id": 2,
            "name": "Mustang Alpha",
            "manufacturer_name": None,
            "quantum_range_gm": 50.0,
            "cargo_capacity_scu": 0.0,
        },
    ]


def _terminals_payload() -> list[dict]:
    """A small but realistic terminal set spanning multiple systems,
    planetoids (planet-only, moon+planet, and neither), and orbital-vs-
    ground status -- mirrors the real shapes CLAUDE.md's Task 12 addendum
    documents (e.g. GrimHEX: both `planet_name`/`moon_name` set; a gateway:
    neither set)."""
    return [
        {
            "id": 1,
            "name": "Lorville - Trade and Development Division",
            "star_system_name": "Stanton",
            "location_name": "Lorville",
            "planet_name": "Hurston",
            "moon_name": None,
            "is_orbital_station": False,
        },
        {
            "id": 2,
            "name": "Everus Harbor",
            "star_system_name": "Stanton",
            "location_name": "Everus Harbor",
            "planet_name": "Hurston",
            "moon_name": None,
            "is_orbital_station": True,
        },
        {
            "id": 3,
            "name": "ArcCorp Mining Area 045",
            "star_system_name": "Stanton",
            "location_name": "ArcCorp Mining Area 045",
            "planet_name": "ArcCorp",
            "moon_name": "Wala",
            "is_orbital_station": False,
        },
        {
            "id": 4,
            "name": "GrimHEX",
            "star_system_name": "Stanton",
            "location_name": "GrimHEX",
            "planet_name": "Crusader",
            "moon_name": "Yela",
            "is_orbital_station": True,
        },
        {
            "id": 5,
            "name": "Terra Gateway (Stanton)",
            "star_system_name": "Stanton",
            "location_name": None,
            "planet_name": None,
            "moon_name": None,
            "is_orbital_station": True,
        },
        {
            "id": 6,
            "name": "Levski",
            "star_system_name": "Nyx",
            "location_name": "Levski",
            "planet_name": None,
            "moon_name": None,
            "is_orbital_station": False,
        },
        {
            "id": 7,
            "name": "Rat's Nest",
            "star_system_name": "Pyro",
            "location_name": "Rat's Nest",
            "planet_name": "Pyro V",
            "moon_name": None,
            "is_orbital_station": False,
        },
    ]


def _mock_status_terminals_ships(terminals: list[dict] | None = None, ships: list[dict] | None = None) -> None:
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_never_run_status())
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        return_value=httpx.Response(200, json=terminals if terminals is not None else [])
    )
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(
        return_value=httpx.Response(200, json=ships if ships is not None else [])
    )


# --- basic rendering / error states ---------------------------------------------


@respx.mock
def test_app_renders_with_no_terminals_yet():
    _mock_status_terminals_ships(terminals=[], ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert any("No terminals available yet" in info.value for info in at.info)


@respx.mock
def test_app_renders_with_no_ships_yet():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=[])

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert any("No ships available yet" in info.value for info in at.info)


@respx.mock
def test_app_shows_error_when_backend_unreachable():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert any("Could not reach the backend" in warning.value for warning in at.sidebar.warning)
    assert any("Could not reach the backend" in error.value for error in at.error)


# --- ship selector (Task 16) -----------------------------------------------------


@respx.mock
def test_ship_selector_populated_from_ships_endpoint():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    ship_box = _find_selectbox(at, "Ship")
    # Manufacturer suffix shown when known; falls back gracefully when null.
    assert ship_box.options == ["Freelancer (MISC)", "Mustang Alpha"]


# --- number-of-hops / starting-budget inputs (Task 16) ---------------------------


@respx.mock
def test_hops_input_is_integer_and_budget_input_is_float():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    hops_box = _find_number_input(at, "Number of hops")
    assert hops_box.min == 1
    assert hops_box.step == 1
    assert isinstance(hops_box.value, int)

    budget_box = _find_number_input(at, "Starting budget (aUEC)")
    assert budget_box.min == 0.0
    assert isinstance(budget_box.value, float)


# --- System / Planetoid / orbital-station filtering (Task 16) --------------------


@respx.mock
def test_system_dropdown_lists_distinct_systems_plus_all():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    system_box = _find_selectbox(at, "System")
    assert system_box.options == ["All", "Nyx", "Pyro", "Stanton"]


@respx.mock
def test_planetoid_dropdown_defaults_to_all_systems_moon_over_planet_precedence():
    """With System left at "All", Planetoid options are derived across every
    terminal: a terminal with both `planet_name`/`moon_name` set (e.g.
    GrimHEX -> Crusader/Yela, ArcCorp Mining Area 045 -> ArcCorp/Wala)
    surfaces its *moon* name, not its planet name -- see
    `frontend.app._terminal_planetoid`'s precedence rationale."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    planetoid_box = _find_selectbox(at, "Planetoid")
    assert planetoid_box.options == ["All", "Hurston", "Pyro V", "Wala", "Yela"]
    assert "ArcCorp" not in planetoid_box.options
    assert "Crusader" not in planetoid_box.options


@respx.mock
def test_planetoid_dropdown_narrows_when_system_selected():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_selectbox(at, "System").select("Stanton").run()

    planetoid_box = _find_selectbox(at, "Planetoid")
    # Pyro V (a Pyro-system-only planetoid) drops out once scoped to Stanton.
    assert planetoid_box.options == ["All", "Hurston", "Wala", "Yela"]


@respx.mock
def test_orbital_station_checkbox_defaults_checked_and_filters_when_unchecked():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_selectbox(at, "System").select("Stanton").run()
    _find_selectbox(at, "Planetoid").select("Hurston").run()

    orbital_checkbox = _find_checkbox(at, "Include orbital stations")
    assert orbital_checkbox.value is True  # default CHECKED

    terminal_box = _find_selectbox(at, "Starting terminal")
    assert terminal_box.options == [
        "Lorville - Trade and Development Division (Stanton)",
        "Everus Harbor (Stanton)",
    ]

    orbital_checkbox.uncheck().run()

    terminal_box = _find_selectbox(at, "Starting terminal")
    assert terminal_box.options == ["Lorville - Trade and Development Division (Stanton)"]


@respx.mock
def test_gateway_terminal_only_reachable_via_planetoid_all():
    """Terra Gateway has neither `planet_name` nor `moon_name` set (CLAUDE.md's
    Task 12 addendum: deep-space jump-point gateways) -- it must disappear
    from the picker as soon as a specific Planetoid is chosen, and reappear
    under "Planetoid: All"."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_selectbox(at, "System").select("Stanton").run()

    terminal_box = _find_selectbox(at, "Starting terminal")
    assert any("Terra Gateway" in option for option in terminal_box.options)

    _find_selectbox(at, "Planetoid").select("Hurston").run()
    terminal_box = _find_selectbox(at, "Starting terminal")
    assert not any("Terra Gateway" in option for option in terminal_box.options)


@respx.mock
def test_filters_can_exclude_every_terminal():
    """GrimHEX is Stanton/Yela's only terminal and is orbital -- unchecking
    "Include orbital stations" with that exact System+Planetoid combination
    leaves nothing to pick, which must be handled gracefully (a warning, no
    crash), not surface a broken/empty selectbox."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_selectbox(at, "System").select("Stanton").run()
    _find_selectbox(at, "Planetoid").select("Yela").run()
    _find_checkbox(at, "Include orbital stations").uncheck().run()

    assert not at.exception
    assert any("No terminals match" in warning.value for warning in at.warning)
    assert not [box for box in at.selectbox if box.label == "Starting terminal"]


@respx.mock
def test_starting_terminal_selectbox_uses_contains_filter_mode():
    """Explicitly requests substring ("contains") matching for the searchable
    start-terminal picker rather than relying on Streamlit's looser default
    "fuzzy" filter mode -- see `frontend/app.py`'s inline comment."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    terminal_box = _find_selectbox(at, "Starting terminal")
    assert terminal_box.proto.filter_mode == SelectWidgetFilterMode_pb2.FILTER_MODE_CONTAINS


# --- route search results (rework: one row per terminal STOP, not per hop) -------
#
# The project owner's reported confusion: `RouteHop.unit_buy_price` is the
# per-unit buy price at the hop's *origin*, `unit_sell_price` at its
# *destination* -- a hop-per-row table hid which terminal a buy/sell
# actually happens at, and never showed a "buy this" instruction for the
# starting terminal at all. `frontend.app._stop_rows` (exercised here only
# indirectly, via the rendered `st.dataframe`, matching this file's existing
# AppTest-only convention -- `frontend/app.py` calls `main()` unconditionally
# at import time, so it can't be imported directly in a normal test) now
# builds one row per terminal *stop*: for `n` hops there are `n + 1` stops --
# stop 0 is the start (buy only), stops `1..n-1` are intermediate (sell what
# was carried in, buy what will be carried out), stop `n` is the end (sell
# only).
#
# `_render_route_results` switched from `st.table` to `st.dataframe` (wider,
# far more numeric columns now) -- results are read via `at.dataframe`, not
# `at.table`.


def _route_response_json(
    *,
    hops: list[dict],
    starting_budget: float,
    final_cash: float,
    total_profit: float,
    total_distance: float,
) -> dict:
    return {
        "found": True,
        "start_terminal_id": 1,
        "hops": hops,
        "total_distance": total_distance,
        "starting_budget": starting_budget,
        "final_cash": final_cash,
        "total_profit": total_profit,
        "message": None,
    }


def _run_route_search(route_response_json: dict):
    """Mocks status/terminals/ships/route, runs the app, clicks "Find best
    route" with the default (first-listed) starting terminal and ship
    selections, and returns the resulting `AppTest`."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())
    respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(200, json=route_response_json)
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_button(at, "Find best route").click().run(timeout=15)
    assert not at.exception
    return at


@respx.mock
def test_app_shows_per_stop_buy_sell_rows_for_worked_example():
    """The exact worked example from the task write-up: a 2-hop route
    (buy Gold at the start, sell it and buy Silver at the middle stop, sell
    Silver at the end), asserting the precise per-stop buy/sell/cash values.
    """
    hops = [
        {
            "terminal_id": 2,
            "terminal_name": "Everus Harbor",
            "commodity_id": 10,
            "commodity_name": "Gold",
            "distance_from_previous": 5.0,
            "quantity_traded": 50.0,
            "unit_buy_price": 20.0,
            "unit_sell_price": 30.0,
            "profit_this_hop": 500.0,
        },
        {
            "terminal_id": 3,
            "terminal_name": "ArcCorp Mining Area 045",
            "commodity_id": 20,
            "commodity_name": "Silver",
            "distance_from_previous": 3.0,
            "quantity_traded": 80.0,
            "unit_buy_price": 10.0,
            "unit_sell_price": 15.0,
            "profit_this_hop": 400.0,
        },
    ]
    at = _run_route_search(
        _route_response_json(
            hops=hops, starting_budget=1000.0, final_cash=1900.0, total_profit=900.0, total_distance=8.0
        )
    )

    assert any("Final cash: 1,900" in success.value for success in at.success)
    assert any("Total profit: 900" in success.value for success in at.success)

    dataframes = at.dataframe
    assert len(dataframes) == 1
    df = dataframes[0].value
    assert len(df) == 3  # start + 2 hop destinations

    # Stop 0 ("Alpha" in the write-up -- the default-selected starting
    # terminal here, "Lorville - Trade and Development Division"): buy 50
    # Gold @ 20.0 (cost 1000.0), nothing to sell yet.
    row0 = df.iloc[0]
    assert row0["Stop"] == 0
    assert row0["Terminal"] == "Lorville - Trade and Development Division"
    assert row0["Sell Commodity"] == "—"
    assert math.isnan(row0["Sell Qty"])
    assert row0["Buy Commodity"] == "Gold"
    assert row0["Buy Qty"] == 50.0
    assert row0["Buy Unit Price"] == 20.0
    assert row0["Buy Cost"] == 1000.0
    assert row0["Cash After"] == 0.0  # 1000.0 - 1000.0

    # Stop 1 ("Bravo"/Everus Harbor): sell 50 Gold @ 30.0 (proceeds 1500.0,
    # profit +500.0), buy 80 Silver @ 10.0 (cost 800.0).
    row1 = df.iloc[1]
    assert row1["Stop"] == 1
    assert row1["Terminal"] == "Everus Harbor"
    assert row1["Distance In"] == 5.0
    assert row1["Sell Commodity"] == "Gold"
    assert row1["Sell Qty"] == 50.0
    assert row1["Sell Unit Price"] == 30.0
    assert row1["Sell Proceeds"] == 1500.0
    assert row1["Sell Profit"] == 500.0
    assert row1["Buy Commodity"] == "Silver"
    assert row1["Buy Qty"] == 80.0
    assert row1["Buy Unit Price"] == 10.0
    assert row1["Buy Cost"] == 800.0
    assert row1["Cash After"] == 700.0  # 0.0 + 1500.0 - 800.0

    # Stop 2 ("Charlie"/ArcCorp Mining Area 045, the final stop): sell 80
    # Silver @ 15.0 (proceeds 1200.0, profit +400.0), nothing more to buy.
    row2 = df.iloc[2]
    assert row2["Stop"] == 2
    assert row2["Terminal"] == "ArcCorp Mining Area 045"
    assert row2["Distance In"] == 3.0
    assert row2["Sell Commodity"] == "Silver"
    assert row2["Sell Qty"] == 80.0
    assert row2["Sell Unit Price"] == 15.0
    assert row2["Sell Proceeds"] == 1200.0
    assert row2["Sell Profit"] == 400.0
    assert row2["Buy Commodity"] == "—"
    assert math.isnan(row2["Buy Qty"])
    assert row2["Cash After"] == 1900.0  # 700.0 + 1200.0

    # The running-cash invariant this task specifically calls out: the last
    # stop's derived running cash must equal `RouteResponse.final_cash`
    # exactly (checked programmatically here, against real rendered
    # response data -- not eyeballed).
    assert math.isclose(row2["Cash After"], 1900.0, rel_tol=1e-9, abs_tol=1e-9)


@respx.mock
def test_app_labels_bridge_hop_clearly_not_blank():
    """A bridge hop (`commodity_id is None`) must read as an explicit
    "repositioning" label, never a blank/confusing cell -- here the second
    (and last) hop is a bridge hop, so the final stop's sell side is
    affected."""
    hops = [
        {
            "terminal_id": 2,
            "terminal_name": "Everus Harbor",
            "commodity_id": 1,
            "commodity_name": "Laranite",
            "distance_from_previous": 10.0,
            "quantity_traded": 50.0,
            "unit_buy_price": 3.5,
            "unit_sell_price": 5.0,
            "profit_this_hop": 75.0,
        },
        {
            "terminal_id": 3,
            "terminal_name": "ArcCorp Mining Area 045",
            "commodity_id": None,
            "commodity_name": None,
            "distance_from_previous": 4.0,
            "quantity_traded": 0.0,
            "unit_buy_price": None,
            "unit_sell_price": None,
            "profit_this_hop": 0.0,
        },
    ]
    at = _run_route_search(
        _route_response_json(
            hops=hops, starting_budget=500.0, final_cash=575.0, total_profit=75.0, total_distance=14.0
        )
    )

    assert any("Final cash: 575" in success.value for success in at.success)
    assert any("Total profit: 75" in success.value for success in at.success)

    df = at.dataframe[0].value
    assert len(df) == 3

    # Stop 0: buy Laranite as normal.
    row0 = df.iloc[0]
    assert row0["Buy Commodity"] == "Laranite"
    assert row0["Buy Cost"] == 175.0  # 50 * 3.5

    # Stop 1 (Everus Harbor): sells the Laranite bought at stop 0 as normal,
    # but the *outgoing* hop (to stop 2) is the bridge hop -- buy side must
    # be clearly labeled, not blank.
    row1 = df.iloc[1]
    assert row1["Sell Commodity"] == "Laranite"
    assert row1["Sell Proceeds"] == 250.0  # 50 * 5.0
    assert row1["Buy Commodity"] == "No purchase — repositioning"
    assert math.isnan(row1["Buy Qty"])
    assert math.isnan(row1["Buy Unit Price"])
    assert math.isnan(row1["Buy Cost"])

    # Stop 2 (ArcCorp Mining Area 045, final stop): the *incoming* hop was
    # the bridge hop -- sell side must be clearly labeled, not blank, and
    # distinct from "no buy" (there's genuinely nothing more to buy here
    # since the route ends, which is a different, unlabeled "—" case).
    row2 = df.iloc[2]
    assert row2["Sell Commodity"] == "No sale — repositioning"
    assert math.isnan(row2["Sell Qty"])
    assert math.isnan(row2["Sell Proceeds"])
    assert row2["Buy Commodity"] == "—"

    assert math.isclose(row2["Cash After"], 575.0, rel_tol=1e-9, abs_tol=1e-9)


@respx.mock
def test_app_shows_single_hop_route_buy_only_then_sell_only():
    """The smallest case, easy to get an off-by-one wrong on: a single-hop
    route has exactly 2 stops -- stop 0 is buy-only (no intermediate stop
    with both a sell and a buy action exists at all), stop 1 is sell-only.
    """
    hops = [
        {
            "terminal_id": 2,
            "terminal_name": "Everus Harbor",
            "commodity_id": 1,
            "commodity_name": "Laranite",
            "distance_from_previous": 10.0,
            "quantity_traded": 20.0,
            "unit_buy_price": 3.5,
            "unit_sell_price": 5.0,
            "profit_this_hop": 30.0,
        },
    ]
    at = _run_route_search(
        _route_response_json(
            hops=hops, starting_budget=100.0, final_cash=130.0, total_profit=30.0, total_distance=10.0
        )
    )

    df = at.dataframe[0].value
    assert len(df) == 2

    row0 = df.iloc[0]
    assert row0["Stop"] == 0
    assert row0["Terminal"] == "Lorville - Trade and Development Division"
    assert row0["Sell Commodity"] == "—"
    assert row0["Buy Commodity"] == "Laranite"
    assert row0["Buy Qty"] == 20.0
    assert row0["Buy Cost"] == 70.0  # 20 * 3.5
    assert row0["Cash After"] == 30.0  # 100.0 - 70.0

    row1 = df.iloc[1]
    assert row1["Stop"] == 1
    assert row1["Terminal"] == "Everus Harbor"
    assert row1["Sell Commodity"] == "Laranite"
    assert row1["Sell Qty"] == 20.0
    assert row1["Sell Proceeds"] == 100.0  # 20 * 5.0
    assert row1["Sell Profit"] == 30.0
    assert row1["Buy Commodity"] == "—"
    assert math.isnan(row1["Buy Qty"])

    # Running-cash invariant, once more on the smallest possible route.
    assert row1["Cash After"] == 130.0
    assert math.isclose(row1["Cash After"], 130.0, rel_tol=1e-9, abs_tol=1e-9)


@respx.mock
def test_app_shows_not_found_message():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())
    respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": False,
                "start_terminal_id": 1,
                "hops": [],
                "total_distance": 0.0,
                "starting_budget": 500.0,
                "final_cash": 500.0,
                "total_profit": 0.0,
                "message": "No profitable route found from this starting terminal under the given constraints.",
            },
        )
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_button(at, "Find best route").click().run(timeout=15)

    assert not at.exception
    assert any("No profitable route found" in info.value for info in at.info)
