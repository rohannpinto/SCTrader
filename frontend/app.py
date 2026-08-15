"""Streamlit frontend for the Star Citizen Trading Route Optimizer.

Talks to the FastAPI backend over local HTTP only -- no direct DB or graph
access from this process (CLAUDE.md: "Frontend is Streamlit, talking to
the backend over local HTTP only"). Run with the backend already running
in a separate terminal:

    uvicorn backend.main:app --reload      # terminal 1
    streamlit run frontend/app.py          # terminal 2

`BACKEND_BASE_URL` defaults to uvicorn's default local address and can be
overridden via the `BACKEND_BASE_URL` environment variable (e.g. if the
backend is running on a different port).

Security note: terminal/commodity names rendered here come from the
external, crowd-sourced wiki/UEX APIs and must be treated as untrusted
display text, never markup -- `unsafe_allow_html=True` is never used
anywhere in this module (CLAUDE.md's standing security ground rules).
Every value from the backend goes through Streamlit's normal (HTML-escaping)
text/table rendering.
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 15.0
REFRESH_TIMEOUT_SECONDS = 120.0  # /refresh can take a while: several external API calls

st.set_page_config(page_title="SC Trading Route Optimizer", layout="wide")


# --- backend calls -------------------------------------------------------------


@st.cache_data(ttl=30)
def _fetch_terminals() -> list[dict]:
    response = httpx.get(f"{BACKEND_BASE_URL}/terminals", timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _fetch_refresh_status() -> dict | None:
    try:
        response = httpx.get(f"{BACKEND_BASE_URL}/refresh-status", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return None


def _trigger_refresh(token: str | None) -> httpx.Response:
    headers = {"X-Refresh-Token": token} if token else {}
    return httpx.post(
        f"{BACKEND_BASE_URL}/refresh", headers=headers, timeout=REFRESH_TIMEOUT_SECONDS
    )


def _search_route(
    start_terminal_id: int, max_distance: float, distance_threshold: float | None
) -> httpx.Response:
    payload: dict = {"start_terminal_id": start_terminal_id, "max_distance": max_distance}
    if distance_threshold is not None:
        payload["distance_threshold"] = distance_threshold
    # Not `raise_for_status()`-ed here: 404/422 carry a `detail` message this
    # UI wants to show the user, not just discard as a generic error.
    return httpx.post(f"{BACKEND_BASE_URL}/route", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)


# --- sidebar: data status + manual refresh --------------------------------------


def _render_sidebar() -> None:
    st.sidebar.header("Data")
    status = _fetch_refresh_status()

    if status is None:
        st.sidebar.warning(
            f"Could not reach the backend at {BACKEND_BASE_URL}. Is it running?"
        )
    else:
        st.sidebar.write(f"Status: **{status['status']}**")
        if status.get("completed_at"):
            st.sidebar.caption(f"Last completed: {status['completed_at']}")
        if status.get("terminals_count") is not None:
            st.sidebar.caption(
                f"{status['terminals_count']} terminals · {status['commodities_count']} commodities · "
                f"{status['prices_count']} prices · {status['distances_count']} distances"
            )
        if status.get("error_message"):
            st.sidebar.error(f"Last refresh error: {status['error_message']}")
        for warning in status.get("warnings", []):
            st.sidebar.caption(f"⚠️ {warning}")

    refresh_token = st.sidebar.text_input("Refresh token (if configured)", type="password")
    if st.sidebar.button("Refresh data now"):
        with st.sidebar:
            with st.spinner("Refreshing... this can take a little while."):
                try:
                    response = _trigger_refresh(refresh_token or None)
                except httpx.HTTPError:
                    st.error(f"Could not reach the backend at {BACKEND_BASE_URL}.")
                else:
                    if response.status_code == 401:
                        st.error("Refresh rejected: missing or invalid token.")
                    elif response.status_code == 409:
                        st.error("A refresh is already in progress.")
                    elif response.status_code == 429:
                        st.error("Too many refresh requests -- please wait a moment and try again.")
                    elif response.status_code != 200:
                        st.error(f"Refresh failed (HTTP {response.status_code}).")
                    else:
                        body = response.json()
                        if body["status"] == "success":
                            st.success("Refresh complete.")
                        else:
                            st.error(f"Refresh failed: {body.get('error_message') or 'unknown error'}")
                        _fetch_terminals.clear()
                        st.rerun()


# --- main: route search ----------------------------------------------------------


def _render_route_results(response: httpx.Response) -> None:
    if response.status_code == 404:
        st.error("Unknown starting terminal.")
        return
    if response.status_code == 422:
        detail = response.json().get("detail", "Invalid request.")
        st.error(detail)
        return
    if response.status_code == 429:
        st.error("Too many route requests -- please wait a moment and try again.")
        return
    if response.status_code != 200:
        st.error(f"Route search failed (HTTP {response.status_code}).")
        return

    body = response.json()
    if not body["found"]:
        st.info(body.get("message") or "No profitable route found from this terminal.")
        return

    st.success(
        f"Total profit: {body['total_profit']:,.0f} aUEC over {body['total_distance']:,.1f} distance "
        f"({len(body['hops'])} hop{'s' if len(body['hops']) != 1 else ''})"
    )
    rows = [
        {
            "Hop": i + 1,
            "Terminal": hop["terminal_name"],
            "Commodity": hop["commodity_name"] or "—",
            "Distance": hop["distance_from_previous"],
            "Profit": hop["profit_this_hop"],
        }
        for i, hop in enumerate(body["hops"])
    ]
    st.table(rows)


def _render_route_search(terminals: list[dict]) -> None:
    if not terminals:
        st.info("No terminals available yet -- trigger a data refresh from the sidebar first.")
        return

    terminal_labels = {
        terminal["id"]: terminal["name"]
        + (f" ({terminal['star_system_name']})" if terminal.get("star_system_name") else "")
        for terminal in terminals
    }
    start_terminal_id = st.selectbox(
        "Starting terminal",
        options=list(terminal_labels.keys()),
        format_func=lambda terminal_id: terminal_labels[terminal_id],
    )

    col1, col2 = st.columns(2)
    with col1:
        max_distance = st.number_input(
            "Max distance budget", min_value=0.01, value=20000.0, step=1000.0
        )
    with col2:
        use_custom_threshold = st.checkbox("Override per-hop distance threshold")
        distance_threshold = (
            st.number_input("Distance threshold", min_value=0.01, value=20000.0, step=1000.0)
            if use_custom_threshold
            else None
        )

    if st.button("Find best route", type="primary"):
        with st.spinner("Searching..."):
            try:
                response = _search_route(start_terminal_id, max_distance, distance_threshold)
            except httpx.HTTPError:
                st.error(f"Could not reach the backend at {BACKEND_BASE_URL}.")
                return
        _render_route_results(response)


def main() -> None:
    st.title("Star Citizen Trading Route Optimizer")
    _render_sidebar()

    st.header("Find a trading route")
    try:
        terminals = _fetch_terminals()
    except httpx.HTTPError:
        terminals = []
        st.error(f"Could not reach the backend at {BACKEND_BASE_URL} to list terminals.")

    _render_route_search(terminals)


main()
