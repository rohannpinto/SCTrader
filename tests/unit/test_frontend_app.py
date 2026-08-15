"""Lightweight smoke tests for `frontend/app.py` via Streamlit's
`AppTest` framework, with backend HTTP calls intercepted by `respx` (same
mocking approach used everywhere else in this test suite) rather than a
real running backend.

These are deliberately shallow: `AppTest` re-executes the script's actual
source each `.run()`, so this exercises real rendering logic, but the goal
here is basic "doesn't crash, shows the right top-level state" coverage,
not a full behavioral spec of every widget interaction.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parent.parent.parent / "frontend" / "app.py")
BACKEND_BASE_URL = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """`@st.cache_data` on `_fetch_terminals()` is a process-wide cache
    keyed by function+args, with no per-`AppTest`-run isolation -- without
    clearing it, whichever test runs first "wins" the cache entry and every
    later test silently reuses that stale result instead of hitting its own
    respx mock.
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


@respx.mock
def test_app_renders_with_no_terminals_yet():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_never_run_status())
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=[]))

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert any("No terminals available yet" in info.value for info in at.info)


@respx.mock
def test_app_shows_error_when_backend_unreachable():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert any("Could not reach the backend" in warning.value for warning in at.sidebar.warning)
    assert any("Could not reach the backend" in error.value for error in at.error)


@respx.mock
def test_app_renders_terminal_selector_when_terminals_available():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_never_run_status())
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "Stanton Gateway", "star_system_name": "Nyx", "location_name": "Levski"},
                {"id": 2, "name": "Terra Mills", "star_system_name": "Stanton", "location_name": None},
            ],
        )
    )

    at = AppTest.from_file(APP_PATH)
    at.run()

    assert not at.exception
    assert len(at.selectbox) == 1
    # AppTest exposes each option already run through `format_func`.
    assert at.selectbox[0].options == ["Stanton Gateway (Nyx)", "Terra Mills (Stanton)"]


@respx.mock
def test_app_shows_found_route_results():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_never_run_status())
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "A", "star_system_name": None, "location_name": None}]
        )
    )
    respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": True,
                "start_terminal_id": 1,
                "hops": [
                    {
                        "terminal_id": 2,
                        "terminal_name": "B",
                        "commodity_id": 1,
                        "commodity_name": "Laranite",
                        "distance_from_previous": 10.0,
                        "profit_this_hop": 100.0,
                    }
                ],
                "total_distance": 10.0,
                "total_profit": 100.0,
                "message": None,
            },
        )
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _find_button(at, "Find best route").click().run(timeout=15)

    assert not at.exception
    assert any("Total profit" in success.value for success in at.success)


@respx.mock
def test_app_shows_not_found_message():
    respx.get(f"{BACKEND_BASE_URL}/refresh-status").mock(
        return_value=httpx.Response(200, json=_never_run_status())
    )
    respx.get(f"{BACKEND_BASE_URL}/terminals").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "A", "star_system_name": None, "location_name": None}]
        )
    )
    respx.post(f"{BACKEND_BASE_URL}/route").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": False,
                "start_terminal_id": 1,
                "hops": [],
                "total_distance": 0.0,
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
