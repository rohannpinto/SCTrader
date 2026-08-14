"""Unit tests for `backend/clients/uex_client.py`.

All non-`live` tests run fully offline: `respx` intercepts every outbound
`httpx` call (assert_all_mocked=True by default), so an accidental real
network call fails the test loudly instead of silently reaching the
internet. Fixtures under `tests/fixtures/uex_*.json` are real, unmodified
responses captured from the live API (`https://api.uexcorp.uk/2.0`) on
2026-08-14 -- see `backend/clients/uex_client.py`'s module docstring for
the full research-spike writeup (reachability, endpoint shape, terminal-ID
join risk, rate limits, and the `orbits_distances` bulk-ingestion strategy
added after the Task 3 review).

The one exception is `test_requests_limit_reached_status_is_retried`, which
is a synthetic body: UEX's docs *document* the `"requests_limit_reached"`
status string but exploration never made enough requests to actually
trigger it live (deliberately -- hammering a real rate limit just to
capture the response isn't a reasonable thing to do to someone else's free
API). That test is clearly marked synthetic in its docstring.

The `@pytest.mark.live` tests at the bottom are excluded from the default
`pytest` run (see `pyproject.toml`) and genuinely hit the real API; both
endpoints used here are confirmed to need no API key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from backend.clients.uex_client import (
    UexApiClientError,
    UexApiTransientError,
    UexClient,
    UexOrbitDistance,
    UexStarSystem,
    UexTerminal,
    _UexTerminalDistanceData,
)
from backend.config import Settings

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
BASE_URL = "https://api.uexcorp.uk/2.0"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _settings(**overrides) -> Settings:
    defaults: dict = dict(
        uex_api_base_url=BASE_URL,
        http_timeout_seconds=1.0,
        http_max_retries=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Replace tenacity's `time.sleep` with a recorder instead of a no-op.

    Retry tests run instantly (no multi-second real waits) while still
    letting assertions check *what* delay was requested.
    """
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))
    return calls


# --- list_terminals, using a real captured fixture -----------------------


@respx.mock
def test_list_terminals_parses_real_fixture():
    # Real, unmodified `GET /terminals?id_star_system=55&type=commodity`
    # response (Nyx system, commodity-capable terminals only -- 9 real
    # terminals, includes both STANTG and LEVSKI referenced elsewhere).
    raw = _load_fixture("uex_terminals_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals", params={"id_star_system": 55, "type": "commodity"}
    ).mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        terminals = client.list_terminals(id_star_system=55, type="commodity")
    finally:
        client.close()

    assert route.called
    assert len(terminals) == 9
    assert all(isinstance(t, UexTerminal) for t in terminals)
    codes = {t.code for t in terminals}
    assert "STANTG" in codes
    assert "LEVSKI" in codes

    stantg = next(t for t in terminals if t.code == "STANTG")
    assert stantg.id == 802
    assert stantg.name == "Admin - Stanton Gateway (Nyx)"
    assert stantg.type == "commodity"
    assert stantg.star_system_name == "Nyx"
    assert stantg.is_available == 1


@respx.mock
def test_list_terminals_no_filters_sends_no_params():
    raw = {"status": "ok", "data": [], "message": ""}
    route = respx.get(f"{BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        terminals = client.list_terminals()
    finally:
        client.close()

    assert route.called
    assert terminals == []
    # No query string at all when no filters are passed.
    assert route.calls.last.request.url.params == httpx.QueryParams()


# --- get_terminal_distance, using real captured fixtures ------------------


@respx.mock
def test_get_terminal_distance_parses_real_fixture():
    # Real, unmodified `GET /terminals_distances?id_terminal_origin=802&
    # id_terminal_destination=778` response (STANTG -> LEVSKI, both real
    # Nyx-system terminals also present in `uex_terminals_sample.json`).
    raw = _load_fixture("uex_terminals_distances_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert route.called
    assert result is not None
    assert result.terminal_id_origin == 802
    assert result.terminal_id_destination == 778
    assert result.terminal_code_origin == "STANTG"
    assert result.terminal_code_destination == "LEVSKI"
    assert result.terminal_name_origin == "Admin - Stanton Gateway (Nyx)"
    assert result.terminal_name_destination == "Levski"
    # The real API sends "distance" as a numeric *string* ("13") despite its
    # own docs declaring it a float -- must still parse to a real float.
    assert result.distance_gm == 13.0
    assert isinstance(result.distance_gm, float)


@respx.mock
def test_get_terminal_distance_returns_none_when_data_is_false():
    # Real, unmodified response for a self-pair
    # (id_terminal_origin == id_terminal_destination == 802): `data` comes
    # back as the JSON literal `false`, not `null` and not an object.
    raw = _load_fixture("uex_terminals_distances_no_data.json")
    respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 802},
    ).mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        result = client.get_terminal_distance(802, 802)
    finally:
        client.close()

    assert result is None


def test_distance_data_coerces_numeric_string_to_float():
    """Direct model-contract check, independent of what the live API
    happened to send: `distance` must parse from a numeric JSON string
    ("6") to a real `float`, since that's the real shape the live API
    sends despite its own docs saying `float`."""
    data = _UexTerminalDistanceData.model_validate(
        {
            "terminal_name_origin": "A",
            "terminal_code_origin": "A",
            "terminal_name_destination": "B",
            "terminal_code_destination": "B",
            "distance": "42",
        }
    )
    assert data.distance == 42.0
    assert isinstance(data.distance, float)


# --- error handling, using a real captured error fixture -------------------


@respx.mock
def test_missing_query_param_400_raises_client_error_without_retry(no_real_sleep):
    # Real, unmodified response for a `terminals_distances` request missing
    # `id_terminal_destination` -- `UexClient.get_terminal_distance` always
    # sends both params, so this is exercised via the low-level
    # `_get_json` to confirm generic >=400 handling against a real body.
    raw = _load_fixture("uex_terminals_distances_missing_param_error.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances", params={"id_terminal_origin": 802}
    ).mock(return_value=httpx.Response(400, json=raw))

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        with pytest.raises(UexApiClientError):
            client._get_json("/terminals_distances", params={"id_terminal_origin": 802})
    finally:
        client.close()

    assert route.call_count == 1
    assert no_real_sleep == []


@respx.mock
def test_orbits_distances_missing_star_system_param_raises_client_error(no_real_sleep):
    # Real, unmodified response for `GET /orbits_distances` with no
    # `id_star_system` at all.
    raw = _load_fixture("uex_orbits_distances_missing_param_error.json")
    route = respx.get(f"{BASE_URL}/orbits_distances").mock(return_value=httpx.Response(400, json=raw))

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        with pytest.raises(UexApiClientError):
            client._get_json("/orbits_distances")
    finally:
        client.close()

    assert route.call_count == 1
    assert no_real_sleep == []


# --- retry / backoff -----------------------------------------------------


@respx.mock
def test_retry_then_success_on_transient_500(no_real_sleep):
    raw = _load_fixture("uex_terminals_distances_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(
        side_effect=[
            httpx.Response(500, json={"status": "error", "http_code": 500, "message": "server error"}),
            httpx.Response(200, json=raw),
        ]
    )

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert result is not None
    assert route.call_count == 2
    assert len(no_real_sleep) == 1


@respx.mock
def test_gives_up_after_max_retries_on_persistent_failure(no_real_sleep):
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(return_value=httpx.Response(500, json={"status": "error", "http_code": 500, "message": "boom"}))

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        with pytest.raises(UexApiTransientError):
            client.get_terminal_distance(802, 778)
    finally:
        client.close()

    # http_max_retries=2 -> 1 initial attempt + 2 retries = 3 total calls.
    assert route.call_count == 3
    assert len(no_real_sleep) == 2


@respx.mock
def test_429_respects_retry_after_header_over_backoff(no_real_sleep):
    raw = _load_fixture("uex_terminals_distances_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow down"}),
            httpx.Response(200, json=raw),
        ]
    )

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert result is not None
    assert route.call_count == 2
    assert no_real_sleep == [2.0]


@respx.mock
def test_requests_limit_reached_status_is_retried(no_real_sleep):
    """UEX's docs document a `"requests_limit_reached"` business-level
    throttle status but don't specify what HTTP status code accompanies it.
    This body is *synthetic* (modeled on the documented shape, not an
    empirically captured response -- see this file's module docstring for
    why) sent with a `200` to prove the check isn't fooled by an
    already-successful HTTP status."""
    raw = _load_fixture("uex_terminals_distances_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "requests_limit_reached", "http_code": 200, "data": None, "message": ""}
            ),
            httpx.Response(200, json=raw),
        ]
    )

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert result is not None
    assert route.call_count == 2
    assert len(no_real_sleep) == 1


@respx.mock
def test_connection_error_is_retried(no_real_sleep):
    raw = _load_fixture("uex_terminals_distances_sample.json")
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=raw),
        ]
    )

    client = UexClient(settings=_settings(http_max_retries=2))
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert result is not None
    assert route.call_count == 2


@respx.mock
def test_timeout_is_retried_then_gives_up(no_real_sleep):
    route = respx.get(
        f"{BASE_URL}/terminals_distances",
        params={"id_terminal_origin": 802, "id_terminal_destination": 778},
    ).mock(side_effect=httpx.ReadTimeout("timed out"))

    client = UexClient(settings=_settings(http_max_retries=1))
    try:
        with pytest.raises(UexApiTransientError):
            client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert route.call_count == 2  # 1 initial + 1 retry
    assert len(no_real_sleep) == 1


# --- optional API key wiring ------------------------------------------------


@respx.mock
def test_api_key_sent_as_bearer_token_when_configured():
    raw = {"status": "ok", "data": [], "message": ""}
    route = respx.get(f"{BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings(uex_api_key="test-key-123"))
    try:
        client.list_terminals()
    finally:
        client.close()

    assert route.calls.last.request.headers["Authorization"] == "Bearer test-key-123"


@respx.mock
def test_no_authorization_header_when_no_key_configured():
    raw = {"status": "ok", "data": [], "message": ""}
    route = respx.get(f"{BASE_URL}/terminals").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings(uex_api_key=None))
    try:
        client.list_terminals()
    finally:
        client.close()

    assert "Authorization" not in route.calls.last.request.headers


# --- terminal-ID join risk, cross-fixture consistency check ---------------


def test_wiki_and_uex_terminal_ids_and_codes_match_for_shared_terminals():
    """Cross-checks Task 2's real wiki fixture against this task's real UEX
    fixture: both reference terminal 802 (STANTG) and 778 (LEVSKI). If UEX
    ever renumbered/recoded a terminal relative to what the wiki API
    exposes, this would catch the drift. As of the fixtures captured
    2026-08-14, they match exactly -- see `uex_client.py`'s module
    docstring for the full join-risk assessment."""
    wiki_raw = _load_fixture("wiki_commodity_laranite.json")
    wiki_entries = {
        e["terminal_id"]: e["terminal_code"] for e in wiki_raw["data"]["uex_prices"]["purchase"]
    }

    uex_raw = _load_fixture("uex_terminals_sample.json")
    uex_terminals = {t["id"]: t["code"] for t in uex_raw["data"]}

    shared_ids = set(wiki_entries) & set(uex_terminals)
    assert {802, 778} <= shared_ids
    for terminal_id in shared_ids:
        assert wiki_entries[terminal_id] == uex_terminals[terminal_id]


# --- list_star_systems / list_available_star_system_ids -------------------


@respx.mock
def test_list_star_systems_parses_real_fixture():
    # Real, unmodified `GET /star_systems` response -- 96 systems total.
    raw = _load_fixture("uex_star_systems_sample.json")
    route = respx.get(f"{BASE_URL}/star_systems").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        systems = client.list_star_systems()
    finally:
        client.close()

    assert route.called
    assert len(systems) == 96
    assert all(isinstance(s, UexStarSystem) for s in systems)
    stanton = next(s for s in systems if s.name == "Stanton")
    assert stanton.id == 68
    assert stanton.is_available == 1


@respx.mock
def test_list_available_star_system_ids_derives_from_is_available_flag():
    """Must not hardcode {55, 64, 68} -- derives them from the live
    `is_available` flag so a newly-live system is picked up automatically."""
    raw = _load_fixture("uex_star_systems_sample.json")
    respx.get(f"{BASE_URL}/star_systems").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        ids = client.list_available_star_system_ids()
    finally:
        client.close()

    assert set(ids) == {55, 64, 68}


@respx.mock
def test_list_available_star_system_ids_picks_up_a_new_system():
    """Same fixture, with one more system flipped to `is_available: 1` --
    proves the derivation is genuinely dynamic, not coincidentally matching
    a hardcoded set."""
    raw = _load_fixture("uex_star_systems_sample.json")
    raw = json.loads(json.dumps(raw))  # deep copy before mutating
    raw["data"][0]["is_available"] = 1
    new_id = raw["data"][0]["id"]
    respx.get(f"{BASE_URL}/star_systems").mock(return_value=httpx.Response(200, json=raw))

    client = UexClient(settings=_settings())
    try:
        ids = client.list_available_star_system_ids()
    finally:
        client.close()

    assert new_id in ids
    assert {55, 64, 68} <= set(ids)


# --- list_orbits_distances / bulk ingestion, using real captured fixtures -


@respx.mock
def test_list_orbits_distances_parses_real_nyx_fixture():
    raw = _load_fixture("uex_orbits_distances_nyx.json")
    route = respx.get(f"{BASE_URL}/orbits_distances", params={"id_star_system": 55}).mock(
        return_value=httpx.Response(200, json=raw)
    )

    client = UexClient(settings=_settings())
    try:
        rows = client.list_orbits_distances(55)
    finally:
        client.close()

    assert route.called
    assert len(rows) == 246
    assert all(isinstance(r, UexOrbitDistance) for r in rows)

    # The specific STANTG-orbit -> LEVSKI-orbit row, real and unmodified.
    row = next(
        r for r in rows if r.id_orbit_origin == 397 and r.id_orbit_destination == 325
    )
    assert row.orbit_origin_name == "Stanton Gateway (Nyx system)"
    assert row.orbit_destination_name == "Delamar"
    assert row.distance_gm == 13.0
    assert isinstance(row.distance_gm, float)


@respx.mock
def test_list_orbits_distances_stanton_row_count():
    # Real, unmodified `GET /orbits_distances?id_star_system=68` response --
    # the densest of the 3 available systems (1548 rows).
    raw = _load_fixture("uex_orbits_distances_stanton.json")
    respx.get(f"{BASE_URL}/orbits_distances", params={"id_star_system": 68}).mock(
        return_value=httpx.Response(200, json=raw)
    )

    client = UexClient(settings=_settings())
    try:
        rows = client.list_orbits_distances(68)
    finally:
        client.close()

    assert len(rows) == 1548


def test_orbit_distance_coerces_numeric_string_to_float():
    """Direct model-contract check: `distance` (aliased `distance_gm`) must
    parse from a numeric JSON string, same real shape as
    `_UexTerminalDistanceData.distance`."""
    row = UexOrbitDistance.model_validate(
        {
            "id_star_system_origin": 55,
            "id_star_system_destination": 55,
            "id_orbit_origin": 397,
            "id_orbit_destination": 325,
            "distance": "13",
        }
    )
    assert row.distance_gm == 13.0
    assert isinstance(row.distance_gm, float)


def test_stanton_asymmetric_orbit_pair_count():
    """Guards the exact count cited in `uex_client.py`'s module docstring
    ("Distances are directional, not reliably symmetric"): recomputes, from
    the real unmodified `uex_orbits_distances_stanton.json` fixture, every
    unordered orbit pair present in both directions and counts how many
    have a different forward vs. reverse `distance`.

    This exists because that exact number was wrong in the docstring once
    already (an earlier draft said 8, conflating directed-row count with
    unordered-pair count -- the real figure is 4 unordered pairs, which is
    8 directed rows since each pair appears once per direction). Asserting
    it here means a future re-capture of the fixture that changes this
    number gets caught by the test suite instead of silently drifting out
    of sync with the docstring."""
    raw = _load_fixture("uex_orbits_distances_stanton.json")
    distance_by_directed_pair = {
        (row["id_orbit_origin"], row["id_orbit_destination"]): float(row["distance"])
        for row in raw["data"]
    }

    seen_unordered_pairs: set[frozenset[int]] = set()
    mismatched_pairs: list[tuple[int, int, float, float]] = []
    for (origin, destination), forward_distance in distance_by_directed_pair.items():
        pair_key = frozenset((origin, destination))
        if pair_key in seen_unordered_pairs:
            continue  # already handled via the reverse-direction row
        seen_unordered_pairs.add(pair_key)
        reverse_distance = distance_by_directed_pair.get((destination, origin))
        if reverse_distance is not None and reverse_distance != forward_distance:
            mismatched_pairs.append((origin, destination, forward_distance, reverse_distance))

    assert len(seen_unordered_pairs) == 774
    assert len(mismatched_pairs) == 4
    # Every mismatched pair involves orbit 359 ("Magnus Gateway"), per the
    # module docstring's qualitative claim.
    assert all(359 in (origin, destination) for origin, destination, _, _ in mismatched_pairs)


@respx.mock
def test_iter_all_orbit_distances_calls_star_systems_then_each_available_system():
    star_systems_raw = _load_fixture("uex_star_systems_sample.json")
    nyx_raw = _load_fixture("uex_orbits_distances_nyx.json")
    stanton_raw = _load_fixture("uex_orbits_distances_stanton.json")

    respx.get(f"{BASE_URL}/star_systems").mock(return_value=httpx.Response(200, json=star_systems_raw))
    nyx_route = respx.get(f"{BASE_URL}/orbits_distances", params={"id_star_system": 55}).mock(
        return_value=httpx.Response(200, json=nyx_raw)
    )
    pyro_route = respx.get(f"{BASE_URL}/orbits_distances", params={"id_star_system": 64}).mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": [], "message": ""})
    )
    stanton_route = respx.get(f"{BASE_URL}/orbits_distances", params={"id_star_system": 68}).mock(
        return_value=httpx.Response(200, json=stanton_raw)
    )

    client = UexClient(settings=_settings())
    try:
        rows = client.list_all_orbit_distances()
    finally:
        client.close()

    assert nyx_route.called
    assert pyro_route.called
    assert stanton_route.called
    # 246 (Nyx, real) + 0 (Pyro, stubbed empty here) + 1548 (Stanton, real).
    assert len(rows) == 246 + 0 + 1548


# --- orbits_distances coverage / end-to-end join check ---------------------


def test_orbits_distances_covers_every_commodity_terminal_pair_in_nyx():
    """Reproduces the reviewer's coverage claim directly from fixtures: every
    pair of distinct orbits among Nyx's real commodity terminals
    (`uex_terminals_sample.json`) has a matching row (in at least one
    direction) in the real `uex_orbits_distances_nyx.json` response. Zero
    gaps, matching the module docstring's 21/21 (42/42 directed) figure."""
    import itertools

    terminals_raw = _load_fixture("uex_terminals_sample.json")
    orbit_ids = {t["id_orbit"] for t in terminals_raw["data"] if t.get("id_orbit")}
    assert len(orbit_ids) == 7  # Nyx's 9 commodity terminals collapse onto 7 orbits

    distances_raw = _load_fixture("uex_orbits_distances_nyx.json")
    pairs_seen = {(row["id_orbit_origin"], row["id_orbit_destination"]) for row in distances_raw["data"]}

    needed_pairs = list(itertools.combinations(sorted(orbit_ids), 2))
    missing = [
        (a, b) for a, b in needed_pairs if (a, b) not in pairs_seen and (b, a) not in pairs_seen
    ]
    assert missing == []


def test_terminal_to_orbit_to_distance_join_matches_direct_terminal_distance():
    """The full end-to-end chain Task 4 will actually perform: wiki
    `terminal_id` -> UEX `terminals.id_orbit` -> `orbits_distances` row --
    cross-checked against the real direct `terminals_distances(802, 778)`
    result (`uex_terminals_distances_sample.json`, distance "13"). All
    three fixtures are real, independently captured responses; they agree."""
    terminals_raw = _load_fixture("uex_terminals_sample.json")
    orbit_by_terminal_id = {t["id"]: t["id_orbit"] for t in terminals_raw["data"]}

    stantg_orbit = orbit_by_terminal_id[802]
    levski_orbit = orbit_by_terminal_id[778]

    orbits_distances_raw = _load_fixture("uex_orbits_distances_nyx.json")
    row = next(
        r
        for r in orbits_distances_raw["data"]
        if r["id_orbit_origin"] == stantg_orbit and r["id_orbit_destination"] == levski_orbit
    )
    orbit_derived_distance = float(row["distance"])

    direct_raw = _load_fixture("uex_terminals_distances_sample.json")
    direct_distance = float(direct_raw["data"]["distance"])

    assert orbit_derived_distance == direct_distance == 13.0


# --- live smoke tests (excluded by default; run with `pytest -m live`) ----


@pytest.mark.live
def test_live_list_terminals_smoke():
    """Hits the real API once. Not a correctness test -- just an early
    warning for schema drift on this specific client. Excluded from the
    default run. Confirmed to need no API key."""
    client = UexClient()
    try:
        terminals = client.list_terminals(id_star_system=55, type="commodity")
    finally:
        client.close()

    assert len(terminals) >= 1
    assert all(t.type == "commodity" for t in terminals)


@pytest.mark.live
def test_live_get_terminal_distance_smoke():
    """Hits the real API once for a known-stable terminal pair (both
    Nyx-system, both referenced in the real captured fixtures). Confirmed
    to need no API key."""
    client = UexClient()
    try:
        result = client.get_terminal_distance(802, 778)
    finally:
        client.close()

    assert result is not None
    assert result.terminal_code_origin == "STANTG"
    assert result.terminal_code_destination == "LEVSKI"
    assert result.distance_gm >= 0


@pytest.mark.live
def test_live_orbits_distances_bulk_ingestion_smoke():
    """Hits the real API for the full recommended bulk-ingestion path:
    discover available systems, then fetch orbit distances for each.
    Confirmed to need no API key. Not a correctness test -- an early
    warning for schema drift or a system/coverage change on UEX's side."""
    client = UexClient()
    try:
        system_ids = client.list_available_star_system_ids()
        assert set(system_ids) >= {55, 64, 68}

        rows = client.list_orbits_distances(68)  # Stanton, the densest
    finally:
        client.close()

    assert len(rows) > 0
    assert all(r.id_star_system_origin == 68 for r in rows)
