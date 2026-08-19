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
display text, never markup -- every value from the backend goes through
Streamlit's normal (HTML-escaping) text/table rendering, never
`unsafe_allow_html=True` (CLAUDE.md's standing security ground rules).

Visual redesign (this task): `_inject_theme_css()` below is the one and
only place in this module that calls `unsafe_allow_html=True`. It injects a
single, developer-authored `<style>` block (background image + dark scrim +
"sci-fi HUD" theme) -- a fixed, literal string this module's author wrote,
never anything derived from a terminal/commodity/ship name or any other
API response value. That is a fundamentally different, low-risk pattern
from the one the rule above forbids (rendering *untrusted external data* as
raw HTML) -- see the comment at `_inject_theme_css()`'s call site for the
full reasoning, and `tests/unit/test_frontend_app.py`'s
`test_unsafe_allow_html_call_sites_are_static_literals_only` for the
regression guard that keeps it that way.

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

Phase 3 (Task 22): a "Risk / reward" slider (0-10, default 10) sends
`RouteRequest.risk_level` alongside the existing route parameters --
`backend/routers/route.py` maps it server-side to the search algorithm's
1-10 `rank` (`rank = max(1, 10 - risk_level)`); this module never computes
`rank` itself. The results area surfaces `RouteResponse.requested_rank`/
`actual_rank_used` back to the user in plain "Nth-most-profitable route"
language rather than echoing those raw 1-10 numbers next to the 0-10
`risk_level` scale the user actually set (the two scales don't line up
1:1, so showing both risks a confusing mismatch -- see `_ordinal`).

Price-report age: the sidebar (`_render_price_data_age`) and the results
area both surface `RefreshStatusOut.price_data_age` -- how old the
underlying crowd-sourced price *reports* are, which is a different and far
more decision-relevant fact than when this app last *fetched* them. See
CLAUDE.md's "Price-report age vs. fetch time" section; showing only the
fetch timestamp made a week-old dataset read as current.
"""

from __future__ import annotations

import base64
import math
import os
import threading
import time
from pathlib import Path

import httpx
import streamlit as st

BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 15.0
REFRESH_TIMEOUT_SECONDS = 120.0  # /refresh can take a while: several external API calls

#: Sentinel option meaning "don't filter on this dimension" in the System/
#: Planetoid dropdowns below. Not a real system/planetoid name, so it can
#: never collide with live data.
_ALL_OPTION = "All"

#: Bundled background image + its attribution, kept together so the code
#: comment and the on-screen credit can never drift apart. See
#: `_background_image_data_uri()` for the full sourcing/license note.
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_BACKGROUND_IMAGE_PATH = _ASSETS_DIR / "background.jpg"
_BACKGROUND_ATTRIBUTION_MARKDOWN = (
    "Background: *Neptune Wide Field (NIRCam)*, NASA/ESA/CSA/STScI "
    "(Webb Space Telescope, 2022) -- public domain, "
    "[source](https://science.nasa.gov/missions/webb/"
    "new-webb-image-captures-clearest-view-of-neptunes-rings-in-decades/)."
)

st.set_page_config(page_title="SC Trading Route Optimizer", page_icon="🚀", layout="wide")


# --- visual theme: background image + dark "sci-fi HUD" styling ----------------
#
# Image sourcing / license (project owner's decision -- see the task brief):
#   Title:   "Neptune Wide Field (NIRCam)"
#   Source:  NASA's official James Webb Space Telescope image gallery,
#            https://science.nasa.gov/missions/webb/
#            new-webb-image-captures-clearest-view-of-neptunes-rings-in-decades/
#   Direct asset (as published by NASA):
#            https://assets.science.nasa.gov/dynamicimage/assets/science/
#            missions/webb/science/2022/09/STScI-01GCVNZ68YTC7FPTBSNA3QDGYW.png
#   Credit:  NASA, ESA, CSA, STScI; Image processing: Joseph DePasquale (STScI),
#            Naomi Rowe-Gurney (NASA-GSFC)
#   License: U.S. government work -- public domain, unrestricted use. NASA's
#            media usage guidelines (https://www.nasa.gov/nasa-brand-center/
#            images-and-media/) ask only that "NASA should be acknowledged as
#            the source of the material," which is why a visible credit is
#            also rendered in the sidebar footer by `_render_sidebar()`
#            (`_BACKGROUND_ATTRIBUTION_MARKDOWN` above), not just recorded
#            here in a comment.
#   Not a photo of Star Citizen itself, deliberately -- CIG's fan content
#   policy doesn't authorize using their marketing material/concept art, so
#   this avoids that category entirely.
#
# Downloaded once (not fetched at runtime -- this app must not depend on an
# external host being reachable) and re-encoded locally: the original
# ~26 MB / 4253x4134 PNG was resized to its long edge at 1920px and saved as
# an ~85%-quality JPEG (`frontend/assets/background.jpg`, ~390 KB, 1920x1866)
# via Pillow, a dependency already pinned in requirements.txt.
@st.cache_data
def _background_image_data_uri() -> str:
    """Base64 data-URI encoding of the bundled background image.

    Computed once per process, not once per script rerun -- Streamlit
    reruns this whole module top-to-bottom on every widget interaction --
    and cached via `@st.cache_data`, the same pattern already used for
    `_fetch_terminals`/`_fetch_ships` below (just with no TTL: the bundled
    file never changes while the process is running).

    A data URI (rather than, say, `st.image` or Streamlit's static-file
    serving) is used so the CSS `background-image` below can reference the
    photo directly with no extra server route/config needed and no
    dependency on where the process's current working directory happens to
    be.
    """
    image_bytes = _BACKGROUND_IMAGE_PATH.read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _inject_theme_css() -> None:
    """Injects one `<style>` block: the fixed-parallax background photo, a
    dark scrim over it for text legibility, and a cohesive dark "sci-fi HUD"
    theme (cool cyan accents, a technical/monospace font for headers and
    widget labels, subtle glow/border styling) for the rest of the UI.

    Parallax effect: pure CSS, scroll-based only -- `background-attachment:
    fixed` (the image stays fixed in the viewport while foreground content
    scrolls over it) plus `background-size: cover` / `background-position:
    center`. Deliberately **not** JS/mouse-tracking-based; that was
    considered and explicitly ruled out for this app.

    SECURITY NOTE, re: CLAUDE.md's "never use `unsafe_allow_html=True` ...
    on any data sourced from the external APIs": that rule exists because
    terminal/commodity/ship names are untrusted, crowd-sourced strings that
    must never be interpreted as markup. The call below is a different
    thing entirely -- a single fixed, developer-authored CSS string. The
    only interpolation anywhere in it is `background_uri`, a base64
    encoding of *this repo's own bundled asset file*
    (`frontend/assets/background.jpg`) computed by
    `_background_image_data_uri()` above -- never a value that traces back
    to a `/terminals`, `/ships`, or `/route` response. Keep it that way: if
    a future change to this function ever needs to interpolate anything
    else in, it must not be request/response data, or this safety argument
    (and the regression test guarding it,
    `tests/unit/test_frontend_app.py::
    test_unsafe_allow_html_call_sites_are_static_literals_only`) no longer
    holds.
    """
    background_uri = _background_image_data_uri()
    st.markdown(
        f"""
        <style>
        :root {{
            color-scheme: dark;
            --sc-panel-bg: rgba(8, 14, 26, 0.72);
            --sc-panel-border: rgba(56, 242, 255, 0.25);
            --sc-sidebar-bg: rgba(6, 11, 20, 0.95);
            --sc-input-bg: rgba(10, 18, 32, 0.85);
            --sc-accent-cyan: #38f2ff;
            --sc-accent-amber: #ffb454;
            --sc-text-primary: #eaf4ff;
            --sc-text-muted: #a9bdd4;
            --sc-font-hud: "Consolas", "Cascadia Mono", "SFMono-Regular",
                "Segoe UI Mono", "Courier New", monospace;
        }}

        /* Full-page fixed parallax background photo + dark scrim gradient,
           so text/widgets laid over a busy astronomical photo stay legible
           (a hard requirement, not polish -- see the module docstring). */
        [data-testid="stAppViewContainer"] {{
            background-image:
                linear-gradient(180deg, rgba(4, 8, 16, 0.88) 0%, rgba(3, 6, 13, 0.94) 100%),
                url("{background_uri}");
            background-size: cover;
            background-position: center center;
            background-attachment: fixed;
            background-repeat: no-repeat;
        }}

        [data-testid="stHeader"] {{
            background: rgba(0, 0, 0, 0);
        }}

        [data-testid="stSidebar"] {{
            background: var(--sc-sidebar-bg);
            border-right: 1px solid var(--sc-panel-border);
        }}

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{
            color: var(--sc-text-primary);
        }}

        /* Main content sits on a translucent dark "HUD panel" over the photo. */
        [data-testid="stMain"] .block-container {{
            background: var(--sc-panel-bg);
            border: 1px solid var(--sc-panel-border);
            border-radius: 16px;
            padding: 2rem 2.5rem 3rem;
            box-shadow: 0 0 48px rgba(0, 0, 0, 0.5);
        }}

        [data-testid="stAppViewContainer"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stAppViewContainer"] [data-testid="stMarkdownContainer"] li,
        [data-testid="stAppViewContainer"] [data-testid="stCaptionContainer"],
        [data-testid="stWidgetLabel"] p {{
            color: var(--sc-text-primary);
        }}

        h1, h2, h3 {{
            font-family: var(--sc-font-hud);
            letter-spacing: 0.03em;
            /* `!important`: Streamlit's own Emotion-generated, class-scoped
               heading rule (e.g. `.st-emotion-cache-xxxx h1`) outranks a
               plain `h1` selector on specificity alone, so a bare
               `color:`/`text-shadow:` here is silently dropped rather than
               applied -- confirmed visually (headers rendered in
               Streamlit's default muted grey, not this accent color, until
               `!important` was added). */
            color: var(--sc-accent-cyan) !important;
            text-shadow: 0 0 16px rgba(56, 242, 255, 0.3);
        }}

        [data-testid="stWidgetLabel"] p {{
            font-family: var(--sc-font-hud);
            letter-spacing: 0.02em;
            color: var(--sc-text-muted);
        }}

        /* Buttons */
        .stButton > button {{
            background: linear-gradient(180deg, rgba(31, 184, 201, 0.22), rgba(15, 30, 48, 0.85));
            border: 1px solid var(--sc-accent-cyan);
            color: var(--sc-text-primary);
            font-family: var(--sc-font-hud);
            letter-spacing: 0.04em;
            border-radius: 6px;
            transition: box-shadow 0.15s ease, background 0.15s ease;
        }}
        .stButton > button:hover {{
            background: linear-gradient(180deg, rgba(56, 242, 255, 0.32), rgba(15, 30, 48, 0.9));
            box-shadow: 0 0 18px rgba(56, 242, 255, 0.45);
            color: var(--sc-accent-cyan);
        }}

        /* Text/number inputs and selectboxes (BaseWeb components) */
        input, textarea {{
            background-color: var(--sc-input-bg) !important;
            color: var(--sc-text-primary) !important;
            border-color: var(--sc-panel-border) !important;
        }}
        [data-baseweb="select"] > div {{
            background-color: var(--sc-input-bg) !important;
            border-color: var(--sc-panel-border) !important;
        }}
        /* Selectbox dropdown popover (rendered in a portal, outside the
           `.stApp` tree -- verified against the real rendered DOM via a
           headless-browser QA pass, since Streamlit's current selectbox
           implementation uses react-aria `role="listbox"`/`role="option"`
           markup, not the older BaseWeb menu markup an earlier draft of
           this rule assumed and which silently matched nothing). */
        [role="listbox"] {{
            background-color: #0d1626 !important;
            border: 1px solid var(--sc-panel-border) !important;
        }}
        [role="listbox"] [role="option"] {{
            background-color: #0d1626 !important;
            color: var(--sc-text-primary) !important;
        }}
        [role="listbox"] [role="option"][aria-selected="true"] {{
            background-color: rgba(56, 242, 255, 0.18) !important;
        }}

        /* Checkbox check-icon box (the div immediately holding the check
           SVG -- structural position, not an auto-generated Emotion class
           name, which Streamlit doesn't treat as stable/public API). */
        [data-testid="stCheckbox"] label > div:first-of-type {{
            border-color: var(--sc-accent-cyan) !important;
        }}

        /* NOTE, deliberately NOT overridden: the slider fill/thumb still
           render in Streamlit's own default accent color. Its DOM (checked
           against the real rendered page) has no `data-testid`/stable
           attribute on the filled-track or thumb elements -- only
           auto-generated Emotion classes Streamlit does not treat as
           public API and which can change across versions. Chasing that
           with brittle positional selectors would risk exactly the
           "fighting Streamlit's CSS too aggressively" failure mode this
           task explicitly warns against, for a purely cosmetic mismatch
           that doesn't affect legibility -- the slider's value bubble and
           track remain fully legible either way. */

        /* Results table + progress bar accents */
        [data-testid="stDataFrame"] {{
            border: 1px solid var(--sc-panel-border);
            border-radius: 10px;
            overflow: hidden;
        }}
        [data-testid="stProgress"] div[role="progressbar"] > div {{
            background-image: linear-gradient(90deg, var(--sc-accent-cyan), var(--sc-accent-amber));
        }}

        ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
        ::-webkit-scrollbar-thumb {{ background: rgba(56, 242, 255, 0.35); border-radius: 6px; }}
        ::-webkit-scrollbar-track {{ background: rgba(4, 8, 16, 0.6); }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --- simulated progress bar (Streamlit has no native "update a bar while a --
# --- blocking call runs" primitive -- see `_run_with_simulated_progress`) ------


def _run_with_simulated_progress(work_fn, *, label: str, estimated_seconds: float):
    """Runs the zero-arg blocking callable `work_fn` (a real backend HTTP
    call) while animating an `st.progress` bar, and returns whatever
    `work_fn` returns (re-raising whatever it raises).

    **Why this approach, not a plain `st.spinner`:** Streamlit reruns this
    whole script top-to-bottom on every interaction and has no built-in way
    to update a widget *while* a single blocking statement is executing --
    a bar can only visibly move between statements the script itself
    executes, one at a time, in order. Since neither `/route` nor `/refresh`
    streams real progress (each is one atomic request/response -- see the
    task brief), "smooth" here has to mean a *simulated* animation that
    keeps advancing while the real call is still in flight, landing at/near
    100% only once the real response actually arrives.

    **How:** the real HTTP call runs on a background `threading.Thread`
    that touches no `st.*` API at all (calling Streamlit APIs off the main
    script thread is unsupported -- it either no-ops or logs a "missing
    ScriptRunContext" warning, since Streamlit's rendering context is
    thread-local to the script's main thread). The main thread -- which
    *does* own the script's rendering context -- creates the progress bar
    once, then polls the worker thread in a short sleep loop, nudging that
    *same* bar element on every tick. Calling `.progress()` again on an
    already-created element updates it in place over the live connection;
    this deliberately never calls `st.rerun()`, which would restart the
    whole script from the top and lose the in-flight request/thread
    entirely -- reruns are the wrong tool here, not a variant worth using.

    **Why it can't stall or look broken either way:** the animated value is
    an asymptotic approach toward 90% (`1 - exp(-elapsed / estimated_seconds)`),
    so it keeps visibly creeping forward the whole time the call is
    outstanding but can never itself claim "done" before the real response
    exists -- correct whether the call finishes in 200ms or 20s. Once
    `work_fn` actually returns (or raises), the bar is snapped straight to
    100% and held just long enough to register, then cleared.
    """
    progress_bar = st.progress(0, text=label)
    result_holder: dict = {}

    def _target() -> None:
        try:
            result_holder["result"] = work_fn()
        except Exception as exc:  # re-raised on the main thread below
            result_holder["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    start = time.monotonic()
    worker.start()

    tick_seconds = 0.05
    while worker.is_alive():
        time.sleep(tick_seconds)
        elapsed = time.monotonic() - start
        fraction = 1.0 - math.exp(-elapsed / estimated_seconds)
        progress_bar.progress(min(90, int(fraction * 90)), text=label)

    worker.join()
    progress_bar.progress(100, text=label)
    time.sleep(0.15)  # let the 100% state register before it disappears
    progress_bar.empty()

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder["result"]


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
    start_terminal_id: int,
    ship_id: int,
    num_hops: int,
    starting_budget: float,
    risk_level: int,
) -> httpx.Response:
    payload = {
        "start_terminal_id": start_terminal_id,
        "ship_id": ship_id,
        "num_hops": num_hops,
        "starting_budget": starting_budget,
        "risk_level": risk_level,
    }
    # Not `raise_for_status()`-ed here: 404/422 carry a `detail` message this
    # UI wants to show the user, not just discard as a generic error.
    return httpx.post(f"{BACKEND_BASE_URL}/route", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)


# --- sidebar: data status + manual refresh --------------------------------------


def _format_age_days(days: float) -> str:
    """Human-readable age, e.g. `0.25` -> `"6 hours"`, `7.04` -> `"7.0 days"`.

    Switches to hours under a day so a genuinely fresh dataset doesn't
    render as an uninformative "0.2 days".
    """
    if days < 1.0:
        hours = max(0, round(days * 24))
        return "less than an hour" if hours == 0 else f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{days:.1f} days"


def _render_price_data_age(status: dict) -> None:
    """Surfaces how old the underlying *price reports* are -- not to be
    confused with when this app last fetched them (`completed_at`, rendered
    separately above this).

    This distinction is the single most decision-relevant fact in the whole
    sidebar and was previously invisible: with the hourly auto-refresh
    scheduler running, "last fetched" is essentially always "minutes ago,"
    which reads as "this data is current." The prices themselves are
    crowd-sourced player observations that are typically *days* old (real
    measured dataset: median ~5-7 days, nothing under 24 hours), and a
    route computed from a week-old price may simply not exist any more when
    the player flies it. That gap is also the entire reason this app has a
    risk/reward slider, so naming it plainly here makes the slider's purpose
    legible instead of mysterious.
    """
    age = status.get("price_data_age")
    if not age:
        return

    st.sidebar.markdown("**Price report age**")
    st.sidebar.caption(
        f"Median: **{_format_age_days(age['median_age_days'])}** old  \n"
        f"Freshest: {_format_age_days(age['min_age_days'])} · "
        f"Stalest: {_format_age_days(age['max_age_days'])}"
    )
    st.sidebar.caption(
        "Prices are crowd-sourced player reports, not a live game feed — "
        "they are typically days old and may have changed in-game since. "
        "Lower the risk/reward slider for routes that are likely less "
        "picked-over."
    )


def _render_sidebar(status: dict | None) -> None:
    st.sidebar.header("Data")

    if status is None:
        st.sidebar.warning(
            f"Could not reach the backend at {BACKEND_BASE_URL}. Is it running?"
        )
    else:
        st.sidebar.write(f"Status: **{status['status']}**")
        if status.get("completed_at"):
            # Deliberately "fetched", not "updated": this is when *this app*
            # last pulled from the external APIs, which says nothing about how
            # old the prices themselves are -- see `_render_price_data_age`.
            st.sidebar.caption(f"Last fetched from APIs: {status['completed_at']}")
        if status.get("terminals_count") is not None:
            st.sidebar.caption(
                f"{status['terminals_count']} terminals · {status['commodities_count']} commodities · "
                f"{status['prices_count']} prices · {status['distances_count']} distances"
            )
        if status.get("error_message"):
            st.sidebar.error(f"Last refresh error: {status['error_message']}")
        for warning in status.get("warnings", []):
            st.sidebar.caption(f"⚠️ {warning}")

        st.sidebar.divider()
        _render_price_data_age(status)

    refresh_token = st.sidebar.text_input("Refresh token (if configured)", type="password")
    if st.sidebar.button("Refresh data now"):
        with st.sidebar:
            try:
                response = _run_with_simulated_progress(
                    lambda: _trigger_refresh(refresh_token or None),
                    label="Refreshing...",
                    # A real refresh hits several external APIs and can
                    # legitimately take a while (REFRESH_TIMEOUT_SECONDS
                    # allows up to 120s) -- a longer estimate keeps the
                    # simulated animation's approach-to-90% pace realistic
                    # instead of parking early and sitting still for most
                    # of a real refresh.
                    estimated_seconds=20.0,
                )
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

    st.sidebar.divider()
    # Visible attribution credit for the bundled background image, per its
    # license terms -- see the code comment at `_inject_theme_css()` for the
    # full source/license record this caption is required to match. Plain
    # `st.sidebar.caption` markdown, not `unsafe_allow_html` -- a hardcoded
    # literal constant, not API-sourced text, but there's no HTML/markup
    # need here at all, so the safer default (Streamlit's own escaping)
    # applies same as everywhere else in this module.
    st.sidebar.caption(_BACKGROUND_ATTRIBUTION_MARKDOWN)


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


def _ordinal(n: int) -> str:
    """English ordinal string for a positive integer, e.g. `3` -> `"3rd"`.

    Used only to phrase the profitability-rank transparency message below
    in "the Nth-most-profitable route" terms -- deliberately *not* the same
    presentation as the 0-10 `risk_level` slider value, since
    `RouteResponse.requested_rank`/`actual_rank_used` live on a different
    1-10 scale (`backend/routers/route.py`: `rank = max(1, 10 -
    risk_level)`) that doesn't line up 1:1 with `risk_level` (e.g.
    `risk_level` 9 and 10 both resolve to `rank=1`). Echoing the raw rank
    number next to a slider the user thinks of in `risk_level` terms would
    invite exactly that confusion, so this only ever surfaces the rank as a
    plain ordinal describing route order, never alongside a `risk_level`
    number.
    """
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _render_rank_transparency(body: dict) -> None:
    """Surfaces `RouteResponse.requested_rank`/`actual_rank_used` (Phase 3)
    so a user who lowered the risk/reward slider -- deliberately asking for
    a route other than the single best one -- can tell whether they got
    what they asked for.

    Stays silent for the common/default case (`requested_rank == 1`, i.e.
    the risk/reward slider left at its default `10`) to match this app's
    plain, uncluttered tone -- there's nothing to reconcile when the user
    never asked for anything but the best route. `actual_rank_used !=
    requested_rank` means `find_best_route` had to clamp down to the
    least-profitable route it *did* find (CLAUDE.md's Phase 3 clamp
    behavior) because fewer than `requested_rank` distinct profitable
    routes existed -- that always gets an explicit warning, never a
    silently-substituted rank.
    """
    requested_rank = body.get("requested_rank", 1)
    actual_rank_used = body.get("actual_rank_used", 1)

    if actual_rank_used != requested_rank:
        route_word = "route" if actual_rank_used == 1 else "routes"
        verb = "was" if actual_rank_used == 1 else "were"
        st.warning(
            f"Only {actual_rank_used} distinct profitable {route_word} {verb} found; "
            "showing the least profitable one found."
        )
    elif requested_rank != 1:
        st.caption(
            f"Showing the {_ordinal(requested_rank)}-most-profitable route found, as requested."
        )


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


def _render_route_results(
    response: httpx.Response, start_terminal_name: str, status: dict | None = None
) -> None:
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
    _render_rank_transparency(body)

    # Repeated here (as well as in the sidebar) on purpose: this is the
    # moment the user is about to commit to flying a route, the sidebar can
    # be collapsed, and the projected profit above is only as good as the
    # age of the prices it was computed from.
    price_age = (status or {}).get("price_data_age")
    if price_age:
        st.caption(
            f"Projected from player-reported prices with a median age of "
            f"{_format_age_days(price_age['median_age_days'])} — actual in-game "
            f"prices may differ."
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


def _render_route_search(
    terminals: list[dict], ships: list[dict], status: dict | None = None
) -> None:
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
        # Same explicit "contains" matching as the starting-terminal picker
        # below, for the same reason and now for consistency between the
        # two: this list is ~237 real ships, far too many to scroll, and
        # Streamlit's default "fuzzy" subsequence matching surfaces
        # surprising hits when typing a partial ship name.
        filter_mode="contains",
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

    col1, col2, col3 = st.columns(3)
    with col1:
        num_hops = st.number_input("Number of hops", min_value=1, value=5, step=1)
    with col2:
        starting_budget = st.number_input(
            "Starting budget (aUEC)", min_value=0.0, value=50_000.0, step=1000.0
        )
    with col3:
        risk_level = st.slider(
            "Risk / reward",
            min_value=0,
            max_value=10,
            value=10,
            help=(
                "10 = the single most profitable route found. Lower values trade "
                "some profit for a route that's presumably less likely to already "
                "be picked over by other players."
            ),
        )

    if st.button("Find best route", type="primary"):
        try:
            response = _run_with_simulated_progress(
                lambda: _search_route(
                    start_terminal_id, ship_id, num_hops, starting_budget, risk_level
                ),
                label="Searching for the best route...",
                estimated_seconds=2.5,
            )
        except httpx.HTTPError:
            st.error(f"Could not reach the backend at {BACKEND_BASE_URL}.")
            return
        _render_route_results(response, terminal_names[start_terminal_id], status)


def main() -> None:
    _inject_theme_css()
    st.title("Star Citizen Trading Route Optimizer")

    # Fetched once per rerun and passed down, rather than re-fetched by each
    # renderer that needs it: the sidebar shows the full status, and the
    # results area reuses the same response's price-age figures to caveat a
    # computed route at the point the user actually acts on it.
    status = _fetch_refresh_status()
    _render_sidebar(status)

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

    _render_route_search(terminals, ships, status)


main()
