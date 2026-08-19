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

Visual redesign (background image + "sci-fi HUD" theme + simulated progress
bars): the functional tests above/below are unchanged in intent -- the
redesign changed only how things look, not what they do, so every existing
assertion here doubles as a regression check that restyling didn't break
real behavior. Two new sections cover what's actually new: the theme CSS
injection itself (`_inject_theme_css`) and a static-analysis regression
guard on every `unsafe_allow_html=True` call site in `frontend/app.py`,
directly enforcing the security reasoning in that module's docstring.
"""

from __future__ import annotations

import ast
import json
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


def _find_slider(at: AppTest, label: str):
    matches = [slider for slider in at.slider if slider.label == label]
    assert len(matches) == 1, f"expected exactly one {label!r} slider, found {len(matches)}"
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


# --- price report age (staleness transparency) ----------------------------------
#
# The distinction these cover is the whole point of the feature: "last
# fetched" (when this app pulled from the external APIs -- with the hourly
# scheduler, almost always minutes ago) is NOT "how old the prices are"
# (crowd-sourced player reports, typically days old). Conflating the two
# made a week-old dataset read as current.


def _status_with_price_age(
    *, median_age_days: float, min_age_days: float = 1.0, max_age_days: float = 30.0
) -> dict:
    status = _never_run_status()
    status.update(
        {
            "status": "success",
            "completed_at": "2026-08-19T02:56:20",
            "price_data_age": {
                "priced_rows": 2004,
                "min_age_days": min_age_days,
                "median_age_days": median_age_days,
                "max_age_days": max_age_days,
            },
        }
    )
    return status


@respx.mock
def test_sidebar_reports_price_report_age_in_days():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_status_with_price_age(median_age_days=7.04))
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "7.0 days" in captions
    # The caveat naming *why* the age matters must accompany the number --
    # a bare "7.0 days" doesn't tell a user the prices may have moved.
    assert "crowd-sourced" in captions


@respx.mock
def test_sidebar_distinguishes_fetch_time_from_report_age():
    """"Last fetched" and "price report age" must be presented as different
    things -- the misleading behavior this feature fixes was showing only
    the former, which reads as "the data is current"."""
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_status_with_price_age(median_age_days=7.0))
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "Last fetched from APIs" in captions
    assert "7.0 days" in captions


@respx.mock
def test_sidebar_formats_sub_day_price_age_in_hours():
    """A genuinely fresh dataset must not render as an uninformative
    "0.2 days"."""
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(
            200, json=_status_with_price_age(median_age_days=0.25, min_age_days=0.25)
        )
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "6 hours" in captions
    assert "0.2 days" not in captions


@respx.mock
def test_sidebar_omits_price_age_when_backend_reports_none():
    """A backend with no priced rows yet (`price_data_age: null`) must
    render no age section at all, not a "0 days old" one."""
    status = _never_run_status()
    status["price_data_age"] = None
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=status)
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "crowd-sourced" not in captions
    assert "days** old" not in captions


@respx.mock
def test_app_still_renders_when_backend_omits_price_data_age_field():
    """Backward compatibility: a `/refresh-status` response with no
    `price_data_age` key at all (an older backend) must not crash the UI."""
    status = _never_run_status()
    status.pop("price_data_age", None)
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=status)
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception


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


# --- risk/reward slider (Phase 3, Task 22) ----------------------------------------


def _minimal_found_route_response_json(**rank_kwargs) -> dict:
    """A single-hop, `found=True` route response -- just enough shape for
    the tests below, which care about the request payload / rank-
    transparency messaging, not the per-stop table itself (already covered
    exhaustively in the "route search results" section further down)."""
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
    return _route_response_json(
        hops=hops,
        starting_budget=100.0,
        final_cash=130.0,
        total_profit=30.0,
        total_distance=10.0,
        **rank_kwargs,
    )


@respx.mock
def test_risk_level_slider_defaults_to_10_and_sends_it_untouched():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())
    route = respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(200, json=_minimal_found_route_response_json())
    )

    at = AppTest.from_file(APP_PATH)
    at.run()

    slider = _find_slider(at, "Risk / reward")
    assert slider.min == 0
    assert slider.max == 10
    assert slider.value == 10  # default preserves pre-Phase-3 behavior

    _find_button(at, "Find best route").click().run(timeout=15)
    assert not at.exception

    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["risk_level"] == 10


@respx.mock
def test_risk_level_slider_moved_value_is_sent_in_request():
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())
    route = respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(200, json=_minimal_found_route_response_json())
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_slider(at, "Risk / reward").set_value(3).run()
    _find_button(at, "Find best route").click().run(timeout=15)
    assert not at.exception

    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["risk_level"] == 3


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
    requested_rank: int = 1,
    actual_rank_used: int = 1,
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
        "requested_rank": requested_rank,
        "actual_rank_used": actual_rank_used,
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
def test_results_caveat_names_price_age_at_the_point_of_decision():
    """The projected-profit figure is only as good as the age of the prices
    it came from, so the results area repeats the staleness caveat -- the
    sidebar can be collapsed, and this is the moment the user commits to
    flying the route."""
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_status_with_price_age(median_age_days=7.04))
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        return_value=httpx.Response(200, json=_terminals_payload())
    )
    respx.get(f"{BACKEND_BASE_URL}/ships").mock(
        return_value=httpx.Response(200, json=_ships_payload())
    )
    respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(200, json=_minimal_found_route_response_json())
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_button(at, "Find best route").click().run(timeout=15)

    assert not at.exception
    assert any(
        "median age of 7.0 days" in caption.value for caption in at.caption
    ), [c.value for c in at.caption]


@respx.mock
def test_results_omit_price_caveat_when_age_unavailable():
    """No price-age data (e.g. a backend that reports `price_data_age:
    null`) means no caveat -- never a fabricated "0 days" claim."""
    at = _run_route_search(_minimal_found_route_response_json())

    assert not any("median age of" in caption.value for caption in at.caption)


@respx.mock
def test_rank_transparency_default_rank_shows_no_extra_messaging():
    """`requested_rank == 1` (the default -- risk/reward slider left at 10)
    is the common case and gets no extra rank messaging at all, matching
    this app's plain/functional tone."""
    at = _run_route_search(_minimal_found_route_response_json())

    assert not list(at.warning)
    assert not any("most-profitable route found" in caption.value for caption in at.caption)


@respx.mock
def test_rank_transparency_non_clamped_rank_shows_plain_caption_no_clamp_warning():
    """`requested_rank == actual_rank_used` but not `1` (the user lowered
    the risk/reward slider and got exactly the rank they asked for): a
    plain caption names the rank, and no clamp warning appears."""
    at = _run_route_search(
        _minimal_found_route_response_json(requested_rank=3, actual_rank_used=3)
    )

    assert any(
        "Showing the 3rd-most-profitable route found, as requested." in caption.value
        for caption in at.caption
    )
    assert not list(at.warning)


@respx.mock
def test_rank_transparency_clamped_rank_shows_explicit_warning():
    """`actual_rank_used != requested_rank` (fewer than `requested_rank`
    distinct profitable routes existed, so `find_best_route` clamped down):
    an explicit warning must say so, not silently show a rank the user
    didn't ask for -- and the plain non-clamped caption must NOT also
    appear."""
    at = _run_route_search(
        _minimal_found_route_response_json(requested_rank=5, actual_rank_used=2)
    )

    assert any(
        "Only 2 distinct profitable routes were found; showing the least profitable one found."
        in warning.value
        for warning in at.warning
    )
    assert not any("as requested" in caption.value for caption in at.caption)


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


# --- visual theme: background image + "sci-fi HUD" styling -----------------------


@respx.mock
def test_theme_css_is_injected_with_background_and_hud_styling():
    """`_inject_theme_css()` must actually run and inject its `<style>`
    block -- checked via `AppTest`'s exposed `Markdown` elements (Streamlit
    represents both `st.markdown` and the `st.write(str)` calls elsewhere in
    this module as `Markdown` elements; `proto.allow_html` is what
    distinguishes an `unsafe_allow_html=True` call from a normal one).

    Spot-checks a handful of the concrete style facts the task brief hard-
    requires -- the CSS-only fixed-parallax background wiring
    (`background-attachment: fixed`, `background-size: cover`) and that the
    bundled image is actually embedded (a `data:image/jpeg;base64,` URI, not
    a remote link) -- rather than the full stylesheet text, so this doesn't
    become unmaintainable as the theme is tuned further.
    """
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    html_markdown = [m for m in at.markdown if m.proto.allow_html]
    assert len(html_markdown) == 1, (
        f"expected exactly one unsafe_allow_html=True markdown element, found {len(html_markdown)}"
    )
    css = html_markdown[0].value
    assert "<style>" in css
    # Scroll-based, CSS-only parallax -- no JS anywhere (explicitly ruled out).
    assert "background-attachment: fixed" in css
    assert "background-size: cover" in css
    assert "background-position: center center" in css
    assert "<script" not in css.lower()
    # The image is bundled/embedded, never a remote hotlink at runtime.
    assert "data:image/jpeg;base64," in css
    assert "http://" not in css and "https://" not in css
    # A dark scrim sits between the photo and foreground content/text.
    assert "linear-gradient" in css


@respx.mock
def test_background_attribution_caption_rendered_in_sidebar():
    """A visible, unobtrusive credit for the bundled background image must
    appear in the sidebar footer, naming NASA/the license -- a real
    licensing condition (see `frontend/app.py`'s comment at
    `_inject_theme_css()`), not optional polish."""
    _mock_status_terminals_ships(terminals=_terminals_payload(), ships=_ships_payload())

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    sidebar_captions = [c.value for c in at.sidebar.caption]
    assert any("NASA" in value and "public domain" in value for value in sidebar_captions)


def test_background_asset_bundled_locally_and_reasonably_sized():
    """The background image must be a real, downloaded file checked into
    the repo -- not a placeholder and not fetched from a remote host at
    runtime (`_inject_theme_css()` reads it via `_BACKGROUND_IMAGE_PATH`
    and embeds it as a data URI, never a network call)."""
    background_path = Path(APP_PATH).resolve().parent / "assets" / "background.jpg"
    assert background_path.is_file(), f"missing bundled background image: {background_path}"

    size_bytes = background_path.stat().st_size
    # A placeholder/broken download would be near-empty; an unresized
    # straight-from-the-source-archive file would be tens of MB (this task's
    # brief asks for compression to roughly 1-2 MB or less). Generous
    # bounds, not an exact pin -- this just catches "someone swapped in a
    # 1x1 placeholder" or "someone bundled the raw, uncompressed original."
    assert 20_000 < size_bytes < 3_000_000, f"unexpected background.jpg size: {size_bytes} bytes"

    # A real JPEG file signature (FF D8 FF), not some other/corrupt format.
    with open(background_path, "rb") as f:
        header = f.read(3)
    assert header == b"\xff\xd8\xff", "background.jpg does not look like a real JPEG file"


# --- unsafe_allow_html regression guard (static analysis) ------------------------


def _unsafe_allow_html_true_calls(tree: ast.Module) -> list[ast.Call]:
    """Every `ast.Call` node anywhere in `tree` that passes
    `unsafe_allow_html=True` as a keyword argument."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "unsafe_allow_html"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                calls.append(node)
                break
    return calls


def _content_arg(call: ast.Call) -> ast.expr:
    """The "body" argument of an `st.markdown(...)`-shaped call -- the first
    positional argument, or the `body=`/`value=` keyword if passed that way.
    """
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg in ("body", "value"):
            return kw.value
    raise AssertionError(f"could not find the content argument on {ast.dump(call)}")


def _names_referenced_in_fstring(node: ast.JoinedStr) -> set[str]:
    """Every bare identifier referenced inside any `{...}` interpolation of
    an f-string -- e.g. `f"a{b}{c.d}"` -> `{"b", "c"}`. Used to check *what*
    is being interpolated, not just *that* something is."""
    names: set[str] = set()
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            for sub in ast.walk(value.value):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def test_unsafe_allow_html_call_sites_are_static_literals_only():
    """Regression guard for the security reasoning in `frontend/app.py`'s
    module docstring and `_inject_theme_css()`'s comment: every
    `unsafe_allow_html=True` call in this module must be a fixed,
    developer-authored string -- a plain literal, or an f-string that only
    ever interpolates `background_uri` (this repo's own bundled asset,
    base64-encoded -- never anything that traces back to a `/terminals`,
    `/ships`, or `/route` response).

    This is a static-analysis check on the source, deliberately not a
    runtime/`AppTest` check: the goal is to catch a *future* change that
    starts interpolating request/response data into one of these calls --
    e.g. a well-meaning "show the selected ship's name in a styled badge"
    change that writes `f"<div>{ship_name}</div>"` into this call. That
    would still "work" and might even look fine in a single test run, which
    is exactly why a runtime check isn't sufficient here; this test fails
    the moment any name outside the allowlist appears inside an
    `unsafe_allow_html=True` call's content, regardless of what value it
    happens to hold when the test runs.
    """
    source = Path(APP_PATH).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=APP_PATH)

    calls = _unsafe_allow_html_true_calls(tree)
    # Exact call-site count, per the task brief -- today there is exactly
    # one (`_inject_theme_css`'s `st.markdown(...)`). A second call site
    # appearing is not automatically wrong, but it must be deliberate and
    # reviewed, not an accidental byproduct of some other change -- bumping
    # this number is the explicit signal that review needs to happen.
    assert len(calls) == 1, (
        f"expected exactly 1 unsafe_allow_html=True call site in {APP_PATH}, "
        f"found {len(calls)} at lines {[c.lineno for c in calls]}. If this "
        "growth is intentional, re-verify each new call site only ever "
        "renders a static, developer-authored literal (see the module "
        "docstring's security note) before updating this test."
    )

    # That one call site must live inside `_inject_theme_css`, not somewhere
    # else -- confirms *where* the reviewed exception applies.
    theme_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_inject_theme_css"
    )
    assert calls[0] in list(ast.walk(theme_fn)), (
        "the sole unsafe_allow_html=True call site must be inside "
        "_inject_theme_css(), not elsewhere in the module"
    )

    allowed_names = {"background_uri"}
    for call in calls:
        content = _content_arg(call)
        if isinstance(content, ast.Constant) and isinstance(content.value, str):
            continue  # a pure literal is always safe
        if isinstance(content, ast.JoinedStr):
            referenced = _names_referenced_in_fstring(content)
            disallowed = referenced - allowed_names
            assert not disallowed, (
                f"unsafe_allow_html=True call at line {call.lineno} interpolates "
                f"{sorted(disallowed)!r} into its content -- only {allowed_names!r} "
                "(this repo's own bundled background-image data URI) may ever be "
                "interpolated into an unsafe_allow_html=True call. If this is "
                "genuinely safe, non-API-sourced data, add it to `allowed_names` "
                "in this test with a comment explaining why; if it traces back to "
                "a /terminals, /ships, or /route response, it must not be here at all."
            )
            continue
        # Anything else (a `.format()` call, string concatenation via `+`,
        # etc.) is exactly the kind of construct that could smuggle dynamic
        # content in without a human noticing -- fail loudly rather than
        # try to enumerate every unsafe shape.
        raise AssertionError(
            f"unsafe_allow_html=True call at line {call.lineno} has a content "
            f"argument that is neither a plain string literal nor a simple "
            f"f-string ({ast.dump(content)}) -- this needs manual review, not "
            "an automatic pass."
        )
