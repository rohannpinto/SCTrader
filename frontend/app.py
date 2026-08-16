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

Security note: terminal/commodity/ship names rendered here come from the
external, crowd-sourced wiki/UEX APIs and must be treated as untrusted
display text, never markup -- `unsafe_allow_html=True` is never used
anywhere in this module (CLAUDE.md's standing security ground rules).
Every value from the backend goes through Streamlit's normal (HTML-escaping)
text/table rendering.

Phase 2 (Task 16): the route form now mirrors the hop-count/cash/cargo
search model (CLAUDE.md's "Route search problem") -- a ship picked from
`GET /ships` (its quantum range/cargo capacity drive the search server-
side), an integer hop budget, and a starting cash balance, replacing
Phase 1's continuous distance-budget controls entirely. The starting-
terminal picker is now filtered by System -> Planetoid -> "include orbital
stations" (derived client-side from the same `GET /terminals` response
already fetched -- CLAUDE.md's Task 12 addendum documents the underlying
`planet_name`/`moon_name`/`is_orbital_station` fields) before being handed
to a searchable `st.selectbox`.
"""

from __future__ import annotations

import math
import os

import httpx
import streamlit as st

BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 15.0
REFRESH_TIMEOUT_SECONDS = 120.0  # /refresh can take a while: several external API calls

#: Sentinel option meaning "don't filter on this dimension" in the System/
#: Planetoid dropdowns below. Not a real system/planetoid name, so it can
#: never collide with live data.
_ALL_OPTION = "All"

st.set_page_config(page_title="SC Trading Route Optimizer", layout="wide")


# --- backend calls -------------------------------------------------------------


@st.cache_data(ttl=30)
def _fetch_terminals() -> list[dict]:
    response = httpx.get(f"{BACKEND_BASE_URL}/terminals", timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=30)
def _fetch_ships() -> list[dict]:
    response = httpx.get(f"{BACKEND_BASE_URL}/ships", timeout=REQUEST_TIMEOUT_SECONDS)
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
    start_terminal_id: int, ship_id: int, num_hops: int, starting_budget: float
) -> httpx.Response:
    payload = {
        "start_terminal_id": start_terminal_id,
        "ship_id": ship_id,
        "num_hops": num_hops,
        "starting_budget": starting_budget,
    }
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
                        _fetch_ships.clear()
                        st.rerun()


# --- starting-terminal filtering (System -> Planetoid -> orbital stations) -----


def _distinct_systems(terminals: list[dict]) -> list[str]:
    """Distinct `star_system_name` values present among `terminals`, sorted.

    A terminal with no known system (`star_system_name is None`) is simply
    omitted from this list -- it's still reachable via "System: All".
    """
    return sorted({t["star_system_name"] for t in terminals if t.get("star_system_name")})


def _terminal_planetoid(terminal: dict) -> str | None:
    """A terminal's "planetoid" for filtering purposes -- the specific body
    it's physically associated with.

    `moon_name` wins over `planet_name` when both are set: CLAUDE.md's Task
    12 addendum shows a terminal with both fields populated is orbiting/
    sitting on the moon specifically (e.g. GrimHEX carries
    `planet_name="Crusader", moon_name="Yela"` -- a player there is at
    Yela, not broadly "at Crusader"; `ArcCorp Mining Area 045` carries
    `planet_name="ArcCorp", moon_name="Wala"` and is literally on Wala's
    surface). Falls back to `planet_name` when only that is set (most
    orbital stations, e.g. Everus Harbor orbits Hurston directly with no
    moon involved). Returns `None` when neither is set -- CLAUDE.md's 19
    deep-space-gateway/Nyx-PSS terminals, which the Planetoid dropdown
    never lists by name; they're only reachable via "Planetoid: All".
    """
    return terminal.get("moon_name") or terminal.get("planet_name")


def _terminals_in_system(terminals: list[dict], system: str) -> list[dict]:
    """`terminals` narrowed to `system`, or all of them when `system` is
    the "All" sentinel."""
    if system == _ALL_OPTION:
        return terminals
    return [t for t in terminals if t.get("star_system_name") == system]


def _distinct_planetoids(terminals: list[dict], system: str) -> list[str]:
    """Distinct planetoid names (see `_terminal_planetoid`) among terminals
    in `system` (or all systems, when `system` is "All"), sorted."""
    scoped = _terminals_in_system(terminals, system)
    return sorted({planetoid for t in scoped if (planetoid := _terminal_planetoid(t))})


def _filter_terminals(
    terminals: list[dict], system: str, planetoid: str, include_orbital_stations: bool
) -> list[dict]:
    """The terminal subset matching the current System/Planetoid/orbital-
    station filter selections -- what populates the start-terminal picker.

    `include_orbital_stations`, when `False`, drops every
    `is_orbital_station=True` terminal from the result; when `True`
    (the default), both ground and orbital terminals matching the other
    filters are included.
    """
    result = []
    for terminal in _terminals_in_system(terminals, system):
        if planetoid != _ALL_OPTION and _terminal_planetoid(terminal) != planetoid:
            continue
        if not include_orbital_stations and terminal.get("is_orbital_station"):
            continue
        result.append(terminal)
    return result


# --- main: route search ----------------------------------------------------------


def _stop_rows(body: dict, start_terminal_name: str) -> list[dict]:
    """One row per terminal *stop* along the route (the start, plus each
    hop's destination) rather than one row per hop.

    `RouteHop.unit_buy_price` is documented as the per-unit buy price at
    the hop's *origin*, `unit_sell_price` at its *destination*
    (`backend/models/schemas.py`) -- so a hop-per-row table hid which
    terminal a buy/sell actually happens at, and never showed a "buy this"
    instruction for the starting terminal at all. For `n = len(hops)` hops
    there are `n + 1` stops: stop `0` is the starting terminal (buy only --
    nothing carried in yet to sell), stops `1..n-1` are intermediate (sell
    whatever was carried in from the previous hop, then buy whatever will
    be carried out to the next one), and stop `n` is the final terminal
    (sell only -- the route ends there, nothing more to buy).

    A hop with `commodity_id is None` is a neutral "bridge" hop (CLAUDE.md's
    Phase 2 search model) -- travelled, but nothing bought/sold on it. That
    renders as an explicit "repositioning" label on whichever side (buy
    and/or sell) it affects, never a blank cell. It's kept distinct from a
    stop that simply has no buy side (the final stop) or no sell side (the
    starting stop) -- those use a plain "—" placeholder instead, since
    there's no hop at all on that side to explain.
    """
    hops = body["hops"]
    n = len(hops)
    rows: list[dict] = []
    cash = body["starting_budget"]

    for i in range(n + 1):
        sell_hop = hops[i - 1] if i >= 1 else None
        buy_hop = hops[i] if i <= n - 1 else None

        if sell_hop is None:
            sell_commodity = "—"
            sell_qty = sell_unit_price = sell_proceeds = sell_profit = None
            distance_in = None
        else:
            distance_in = sell_hop["distance_from_previous"]
            if sell_hop["commodity_id"] is None:
                sell_commodity = "No sale — repositioning"
                sell_qty = sell_unit_price = sell_proceeds = sell_profit = None
            else:
                sell_commodity = sell_hop["commodity_name"]
                sell_qty = sell_hop["quantity_traded"]
                sell_unit_price = sell_hop["unit_sell_price"]
                sell_proceeds = sell_qty * sell_unit_price
                sell_profit = sell_hop["profit_this_hop"]

        if buy_hop is None:
            buy_commodity = "—"
            buy_qty = buy_unit_price = buy_cost = None
        elif buy_hop["commodity_id"] is None:
            buy_commodity = "No purchase — repositioning"
            buy_qty = buy_unit_price = buy_cost = None
        else:
            buy_commodity = buy_hop["commodity_name"]
            buy_qty = buy_hop["quantity_traded"]
            buy_unit_price = buy_hop["unit_buy_price"]
            buy_cost = buy_qty * buy_unit_price

        cash += sell_proceeds or 0.0
        cash -= buy_cost or 0.0

        rows.append(
            {
                "Stop": i,
                "Terminal": start_terminal_name if i == 0 else sell_hop["terminal_name"],
                "Distance In": distance_in,
                "Sell Commodity": sell_commodity,
                "Sell Qty": sell_qty,
                "Sell Unit Price": sell_unit_price,
                "Sell Proceeds": sell_proceeds,
                "Sell Profit": sell_profit,
                "Buy Commodity": buy_commodity,
                "Buy Qty": buy_qty,
                "Buy Unit Price": buy_unit_price,
                "Buy Cost": buy_cost,
                "Cash After": cash,
            }
        )

    # Correctness check, not a cosmetic nicety: the running cash derived
    # stop-by-stop above must land exactly on `final_cash` (within float
    # tolerance). Every `Cash After` value telescopes solely from
    # `sell_proceeds`/`buy_cost` against `starting_budget`, so this either
    # holds by construction or the derivation above has a real bug -- fail
    # loudly rather than silently render a wrong number, matching this
    # codebase's existing style of hard-checking cross-field invariants
    # (see `RouteResponse`'s own `model_validator`s in
    # `backend/models/schemas.py`).
    final_cash = body["final_cash"]
    if not math.isclose(rows[-1]["Cash After"], final_cash, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(
            "Route results table: derived running cash "
            f"({rows[-1]['Cash After']!r}) does not reconcile with "
            f"RouteResponse.final_cash ({final_cash!r})."
        )
    return rows


def _render_route_results(response: httpx.Response, start_terminal_name: str) -> None:
    if response.status_code == 404:
        st.error("Unknown starting terminal or ship.")
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
        f"Final cash: {body['final_cash']:,.0f} aUEC  ·  "
        f"Total profit: {body['total_profit']:,.0f} aUEC  ·  "
        f"{body['total_distance']:,.1f} distance travelled "
        f"({len(body['hops'])} hop{'s' if len(body['hops']) != 1 else ''})"
    )

    rows = _stop_rows(body, start_terminal_name)

    # `st.dataframe`, not `st.table`: one row per stop now spans separate
    # buy/sell/cash columns (wider and far more numeric than the old
    # one-row-per-hop table), so it benefits from `st.dataframe`'s
    # scrollable container and per-column numeric formatting via
    # `column_config`. It also sidesteps a known `st.table` quirk (a column
    # mixing floats with a "—" string placeholder gets silently stringified
    # whole) -- the "—"/"repositioning" placeholders here live only in the
    # text `*Commodity` columns; every numeric column stays `float | None`
    # (rendered as blank by `st.dataframe`, not a placeholder string), so
    # no numeric column ever mixes types.
    st.dataframe(
        rows,
        hide_index=True,
        column_config={
            "Stop": st.column_config.NumberColumn(format="%d"),
            "Distance In": st.column_config.NumberColumn(
                format="%.1f", help="Distance travelled from the previous stop to reach this one."
            ),
            "Sell Qty": st.column_config.NumberColumn(format="%.1f"),
            "Sell Unit Price": st.column_config.NumberColumn(format="%.2f"),
            "Sell Proceeds": st.column_config.NumberColumn(format="%.2f"),
            "Sell Profit": st.column_config.NumberColumn(format="%.2f"),
            "Buy Qty": st.column_config.NumberColumn(format="%.1f"),
            "Buy Unit Price": st.column_config.NumberColumn(format="%.2f"),
            "Buy Cost": st.column_config.NumberColumn(format="%.2f"),
            "Cash After": st.column_config.NumberColumn(
                format="%.2f", help="Running cash on hand after this stop's sell/buy."
            ),
        },
    )


def _render_route_search(terminals: list[dict], ships: list[dict]) -> None:
    if not terminals:
        st.info("No terminals available yet -- trigger a data refresh from the sidebar first.")
        return
    if not ships:
        st.info("No ships available yet -- trigger a data refresh from the sidebar first.")
        return

    ship_labels = {
        ship["id"]: ship["name"]
        + (f" ({ship['manufacturer_name']})" if ship.get("manufacturer_name") else "")
        for ship in ships
    }
    ship_id = st.selectbox(
        "Ship",
        options=list(ship_labels.keys()),
        format_func=lambda ship_id: ship_labels[ship_id],
    )

    st.subheader("Starting terminal")
    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        system = st.selectbox("System", options=[_ALL_OPTION] + _distinct_systems(terminals))
    with filter_col2:
        planetoid = st.selectbox(
            "Planetoid", options=[_ALL_OPTION] + _distinct_planetoids(terminals, system)
        )
    with filter_col3:
        include_orbital_stations = st.checkbox("Include orbital stations", value=True)

    filtered_terminals = _filter_terminals(terminals, system, planetoid, include_orbital_stations)
    if not filtered_terminals:
        st.warning("No terminals match the current System / Planetoid / orbital-station filters.")
        return

    terminal_labels = {
        terminal["id"]: terminal["name"]
        + (f" ({terminal['star_system_name']})" if terminal.get("star_system_name") else "")
        for terminal in filtered_terminals
    }
    # Raw (unsuffixed) display names, keyed by id -- threaded through to the
    # results table so it can show "buy this at <starting terminal name>"
    # without the backend needing to echo the name back itself; the user
    # just picked it from this very selectbox.
    terminal_names = {terminal["id"]: terminal["name"] for terminal in filtered_terminals}
    start_terminal_id = st.selectbox(
        "Starting terminal",
        options=list(terminal_labels.keys()),
        format_func=lambda terminal_id: terminal_labels[terminal_id],
        # Guarantees case-insensitive substring ("contains") matching while
        # typing -- Streamlit's default `filter_mode` ("fuzzy") is a looser
        # in-order-subsequence match that would also happen to satisfy a
        # middle-of-the-name substring search, but "contains" is the precise
        # behavior actually wanted here, so it's requested explicitly rather
        # than relied on implicitly.
        filter_mode="contains",
    )

    col1, col2 = st.columns(2)
    with col1:
        num_hops = st.number_input("Number of hops", min_value=1, value=5, step=1)
    with col2:
        starting_budget = st.number_input(
            "Starting budget (aUEC)", min_value=0.0, value=50_000.0, step=1000.0
        )

    if st.button("Find best route", type="primary"):
        with st.spinner("Searching..."):
            try:
                response = _search_route(start_terminal_id, ship_id, num_hops, starting_budget)
            except httpx.HTTPError:
                st.error(f"Could not reach the backend at {BACKEND_BASE_URL}.")
                return
        _render_route_results(response, terminal_names[start_terminal_id])


def main() -> None:
    st.title("Star Citizen Trading Route Optimizer")
    _render_sidebar()

    st.header("Find a trading route")
    try:
        terminals = _fetch_terminals()
    except httpx.HTTPError:
        terminals = []
        st.error(f"Could not reach the backend at {BACKEND_BASE_URL} to list terminals.")

    try:
        ships = _fetch_ships()
    except httpx.HTTPError:
        ships = []
        st.error(f"Could not reach the backend at {BACKEND_BASE_URL} to list ships.")

    _render_route_search(terminals, ships)


main()
