"""Client for UEX Corp's public API 2.0 (`terminals`, `terminals_distances`,
`orbits_distances`, `star_systems`).

Verified empirically against the live API (2026-08-14) -- see
`tests/fixtures/uex_*.json` for real, unmodified captured responses and
`tests/unit/test_uex_client.py` for the respx-mocked test suite built on top
of them. This module's research spike also settled the open questions from
the design plan; the findings are documented section by section below.

**Corrected picture (post-review):** the first pass of this research spike
missed a bulk endpoint, `orbits_distances`, documented right alongside
`terminals_distances` in UEX's own docs sidebar. It changes the recommended
ingestion strategy materially -- see "Bulk distance ingestion: `GET
/orbits_distances` + `id_orbit` join" below, which supersedes the original
"one `terminals_distances` call per pair" plan for anything beyond a
one-off spot lookup.

Reachability -- two different UEX domains, two different outcomes
-------------------------------------------------------------------
`backend/config.py`'s original placeholder base URL,
``https://uexcorp.space/api/2.0``, fronts the real API but sits behind
Cloudflare with bot-management enabled. A bare/default-UA request (`curl`
with no `-A`, or a bare `httpx.get`) gets an HTTP `403` challenge page. That
part matches the prior research's guess. What does **not** match the guess:
this is not a simple User-Agent string check that a browser-like header
fixes. `curl` with a full Chrome `User-Agent` string sails through (`200`),
but Python `httpx` -- with the *exact same* `User-Agent`, plus a full set of
`Accept`/`Accept-Language`/`Accept-Encoding`/`Sec-Fetch-*`/`Referer` headers
copied from a real browser request -- still gets `403` with a
`cf-mitigated: challenge` response header. This is Cloudflare fingerprinting
the TLS/HTTP client itself (JA3/JA4-style), not reading the `User-Agent`
header at all; no combination of headers sent from `httpx` gets past it.

UEX Corp also serves the identical API from a second domain,
``https://api.uexcorp.uk/2.0`` -- it's the canonical example URL used
throughout UEX's own API docs (`https://uexcorp.space/api/documentation`,
itself only reachable with a browser-like `User-Agent` via `curl`, same
Cloudflare check as the API on that domain). `api.uexcorp.uk` has no such
block: plain `httpx.get(...)` with no special headers at all returns `200`
with the same JSON payloads, same `id`/`code` values, same everything.
`backend/config.py`'s `uex_api_base_url` default was corrected to
``https://api.uexcorp.uk/2.0`` accordingly (see that file's inline
comment). **No API key is required** for any endpoint this client uses --
confirmed two ways: (1) every request below succeeded with zero
`Authorization` header and zero query-string credential, and (2) UEX's own
docs mark endpoints that need a Bearer token with a lock icon; `terminals`,
`terminals_distances`, `star_systems`, and `commodities*` all lack that
icon, while personal/write endpoints (`user_trades`, `wallet_*`,
`marketplace_negotiations`, `fleet`, ...) have it. `get_settings()
.uex_api_key`, when set, is still wired in below as a `Bearer` token (UEX's
documented general auth mechanism) so this client transparently starts
authenticating if UEX ever locks these endpoints down -- but as of this
writing it is not needed and defaults to `None`.

`terminals_distances` is a single-pair lookup, NOT a bulk matrix
-------------------------------------------------------------------
This is the single most important shape finding, and it overturns the
"maybe it's a full pairwise matrix, maybe paginated" open question from the
design plan: it is **neither**. `GET /terminals_distances` *requires* both
`id_terminal_origin` and `id_terminal_destination` as query params --
omitting either gets a `400` with `status: "missing_id_terminal_origin"` /
`"missing_id_terminal_destination"` (see
`tests/fixtures/uex_terminals_distances_missing_param_error.json`, a real
captured response). There is no "list all distances", no pagination, no
`id_star_system`-only scoping -- every pair must be requested individually.
Confirmed by UEX's own docs page for this endpoint (fetched with a
browser-like `User-Agent`, `https://uexcorp.space/api/documentation/id/
get_terminals_distances/`): "Estimate the distance (in gigameters) between
two terminals", `Input: id_terminal_origin (int), id_terminal_destination
(int)` -- both listed as required, no other input.

Taken in isolation this would be a real performance problem for Task 4's
ingestion pipeline: `GET /terminals` returns **826** total terminals, of
which **161** have `type == "commodity"` and are plausible trade-graph
nodes. A full pairwise distance fetch across just the commodity terminals
via `terminals_distances` alone would be up to 161*160/2 = 12,880
individual HTTP requests -- at UEX's documented **120 requests/minute** cap
(see "Rate limiting" below), on the order of ~1.8 hours serially. **This is
no longer the recommended approach for bulk ingestion** -- see the next
section, `orbits_distances`, which gets the same coverage in single digits
of requests. `terminals_distances` remains useful as a cheap single-pair
spot-check/fallback (e.g. verifying one `orbits_distances`-derived distance
against a direct lookup, as this client's tests do), just not as the bulk
ingestion path.

`distance` is a JSON *string*, not a float, despite the docs
-------------------------------------------------------------------
UEX's own docs declare `distance: float`, but every real captured response
sends it as a numeric string (`"distance":"6"`, `"13"`, `"64"`, `"112"`),
in gigameters. `UexTerminalDistance.distance_gm` is typed `float` anyway --
pydantic v2's default (lax) validation mode coerces a numeric string to
`float` transparently, so this is handled without any custom validator, but
is called out here since a `strict=True` model config would silently break
on every real response.

`data: false` means "no distance known for this pair" (not just self-pairs)
-------------------------------------------------------------------
A successful (`200`, `status: "ok"`) response's `data` field is normally an
object, but comes back as the JSON literal `false` (not `null`, not an
empty object) both for a self-pair (`id_terminal_origin ==
id_terminal_destination`) and for a syntactically valid but nonexistent
terminal ID. `_UexTerminalDistanceResponse.data` is typed
`_UexTerminalDistanceData | bool | None` to parse both shapes without
raising; `UexClient.get_terminal_distance()` returns `None` in the `False`
case rather than raising, since this is a normal "not available" outcome,
not an error.

Bulk distance ingestion: `GET /orbits_distances` + `id_orbit` join
-------------------------------------------------------------------
`GET /orbits_distances?id_star_system={id}` returns **every** orbit-pair
distance within that star system in a single response -- no pagination, no
per-pair querying. An "orbit" here is UEX's term for a station/POI's
parent celestial waypoint (a planet's Lagrange point, a moon, a jump-point
gateway, etc.) -- coarser-grained than a terminal, but every terminal
belongs to exactly one (`id_orbit` on a `/terminals` row).

This is a bulk endpoint, not a per-pair one, and gets full terminal
distance coverage in a handful of requests instead of thousands:

- Only **3** star systems are currently "available"/playable:
  `is_available == 1` on `/star_systems` selects exactly Nyx (55), Pyro
  (64), and Stanton (68) -- every other of the 96 total systems in the
  dataset is `is_available: 0` (not yet live in-game). `UexClient
  .list_available_star_system_ids()` derives this set from live data
  rather than a hardcoded literal, so a new system going live in-game
  going forward is picked up automatically, no code change required (a
  fourth call to `/star_systems` is the only extra request needed to
  discover this -- cheap either way, even if the set never changes).
- 3 calls to `orbits_distances` (one per available system) returned 1548
  (Stanton), 1724 (Pyro), and 246 (Nyx) rows respectively.
- Of the 161 commodity terminals, 160 carry a non-zero `id_orbit` (the one
  exception, terminal id 422 "Admin - UEX Station" in Stanton, has
  `id_orbit: 0` -- no parent orbit on record; Task 4 will need to decide
  how to handle this single terminal, e.g. exclude it or fall back to
  `terminals_distances` for any edge touching it specifically). Those 160
  terminals collapse onto just **49** distinct orbits across the 3 systems
  (25 in Stanton, 17 in Pyro, 7 in Nyx).
- Coverage is complete: every one of the orbit pairs needed to connect all
  commodity terminals within each system is present in that system's
  `orbits_distances` response -- verified directly (not just spot-checked)
  by computing all `C(n, 2)` orbit pairs per system from the terminal data
  and confirming each has a matching row (in at least one direction) in
  the corresponding `orbits_distances` response: Nyx 21/21 unordered pairs
  (42/42 directed rows), Pyro 136/136 (272/272 directed), Stanton 300/300
  (600/600 directed). Zero gaps.
- **Distances are directional, not reliably symmetric.** Most orbit pairs
  have identical `distance` in both directions, but Stanton has 4 (of 774
  unordered pairs, each present in both directions -- 1548 directed rows
  total) where the forward and reverse `distance` genuinely differ -- not a
  rounding artifact (e.g. orbit 359 "Magnus Gateway (Stanton system)" <->
  orbit 337 "Hurston Lagrange Point 4" is `73` one way and `56` the other).
  All 4 mismatched pairs (8 directed rows, since each pair contributes one
  row per direction) involve orbit 359, a jump-point gateway orbit, where
  the routing UEX computes is plausibly asymmetric for real in-universe
  reasons (jump transit vs. normal flight). This exact count -- 4
  mismatched unordered pairs -- is asserted directly against the real
  `uex_orbits_distances_stanton.json` fixture by `test_uex_client.py`'s
  `test_stanton_asymmetric_orbit_pair_count`, so it can't silently drift if
  UEX's data changes. **Do not assume symmetry** -- look up (or store)
  both directions, or explicitly decide a symmetry-reduction rule, rather
  than reusing one direction's value for the other.
- Sanity-checked end to end against a real `terminals_distances` result:
  terminal `STANTG` (id 802, `id_orbit: 397`) to terminal `LEVSKI` (id 778,
  `id_orbit: 325`), both in Nyx, resolve via `orbits_distances` to the
  orbit pair `(397, 325)` with `distance: "13"` -- exactly matching the
  direct `terminals_distances(802, 778)` result (also `"13"`) captured
  earlier. This is also why terminal-level and orbit-level distance can be
  used interchangeably for terminals that don't share an orbit: UEX's
  terminal-to-terminal distance appears to be inherited directly from the
  parent orbit pair, not computed per-terminal.
- `UexClient.list_orbits_distances(star_system_id)` wraps one call;
  `UexClient.iter_all_orbit_distances()` / `.list_all_orbit_distances()`
  chain `list_available_star_system_ids()` with one `list_orbits_distances`
  call per system -- the full recommended bulk-ingestion path, 4 total
  requests as of this writing (1 for `/star_systems` + 3 for
  `/orbits_distances`) for orbit-level coverage of the whole game.
- Real, unmodified fixtures: `tests/fixtures/uex_orbits_distances_stanton
  .json` (the densest system, 1548 rows) and `..._nyx.json` (246 rows, used
  in the end-to-end join test above since it's small and contains both
  `STANTG`/`LEVSKI`). `tests/fixtures/uex_star_systems_sample.json` is the
  full real `/star_systems` response (96 systems) backing
  `list_available_star_system_ids()`'s tests.

Terminal-ID join risk (CLAUDE.md's flagged concern) -- resolved, clean join
-------------------------------------------------------------------
CLAUDE.md and this task both flagged whether the wiki API's
`terminal_id`/`terminal_code` (seen in `tests/fixtures/wiki_commodity_
laranite.json` etc., from Task 2) would line up with UEX's own terminal
identifiers, or whether Task 4 would need a name+system fallback join.
Checked directly: every `terminal_id`/`terminal_code` pair across all 26
purchase entries in the real `wiki_commodity_laranite.json` fixture matches
UEX's `id`/`code` for that same terminal, exactly, field for field (e.g.
wiki's `terminal_id: 802, terminal_code: "STANTG"` <-> UEX's `id: 802, code:
"STANTG"`, both naming "Admin - Stanton Gateway (Nyx)") -- 26/26 matched.
This makes sense structurally: the wiki API's price data is explicitly
sourced from UEX (`uex_prices` is the very key it's nested under in Task
2's client), so it re-exposes UEX's own terminal IDs verbatim rather than
minting its own. **Task 4 can join on `terminal_id == id` directly; the
name+system fallback in the design plan's contingency should not be
needed.** (`tests/fixtures/uex_terminals_sample.json`, a real captured
`/terminals?id_star_system=55&type=commodity` response, includes both
`STANTG` (802) and `LEVSKI` (778) -- the same two terminals referenced in
`test_uex_client.py`'s cross-fixture join-consistency test.)

Rate limiting
-------------
UEX's general API docs page documents **120 requests/minute** (172,800/day)
as the quota, and a documented (but not empirically triggered -- this
client made a deliberately small number of requests during exploration,
never enough to trip it) `"requests_limit_reached"` status string for
over-quota requests. Since the docs don't specify what HTTP status code
accompanies that status string (unlike, e.g., the documented `500` for
`"error"`), `_perform_request` below treats `status ==
"requests_limit_reached"` as transient/retryable **regardless of the HTTP
status code the response carried** (defensively -- if UEX sends it with
`200`, as sloppier APIs sometimes do for business-level errors, a
status-code-only check would silently treat a throttled response as
success). A `429` is *also* treated as transient the same way `wiki_client`
does, honoring `Retry-After` when present, in case UEX ever sends a
standard `429` instead of/in addition to the documented body shape.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)


# --- External API contract models -------------------------------------------
#
# These describe the *UEX API's* response shape, deliberately kept separate
# from `backend/models/db.py` / `backend/models/schemas.py` (our internal
# contract) -- Task 4's ingestion pipeline is what translates between them.
# Every model below uses `extra="ignore"` so additions to the live schema
# never break parsing; only the fields this app actually needs are declared.


class UexTerminal(BaseModel):
    """One entry from `GET /terminals`.

    UEX's `is_available` / `is_available_live` / `is_visible` are documented
    (and observed) as `0`/`1` integers, not JSON booleans -- kept as `int`
    rather than coerced to `bool` so the distinction between "not modeled in
    pydantic" and "genuinely absent" can't get muddled; callers filter on
    these explicitly.

    `id_orbit` (added for Task 4's distance-expansion algorithm) is present
    on every real `/terminals` row observed but was omitted from the first
    pass of this model; the raw value `0` is UEX's documented sentinel for
    "no parent orbit on record" (e.g. terminal 422, "Admin - UEX Station")
    -- callers must not treat it as a real orbit id.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    code: str
    name: str
    nickname: str | None = None
    displayname: str | None = None
    type: str
    id_star_system: int | None = None
    id_orbit: int | None = None
    star_system_name: str | None = None
    planet_name: str | None = None
    orbit_name: str | None = None
    moon_name: str | None = None
    space_station_name: str | None = None
    outpost_name: str | None = None
    city_name: str | None = None
    is_available: int = 0
    is_available_live: int = 0
    is_visible: int = 0


class _UexTerminalDistanceData(BaseModel):
    """Raw `data` object shape from a successful `/terminals_distances` call.

    Deliberately does *not* include the terminal IDs -- UEX's response
    doesn't echo them back, only names/codes (see module docstring). The
    public `UexTerminalDistance` model below adds them back in from what was
    requested.
    """

    model_config = ConfigDict(extra="ignore")

    orbit_name_origin: str | None = None
    terminal_name_origin: str
    terminal_nickname_origin: str | None = None
    terminal_code_origin: str
    orbit_name_destination: str | None = None
    terminal_name_destination: str
    terminal_nickname_destination: str | None = None
    terminal_code_destination: str
    distance: float  # gigameters; sent as a numeric string by the live API


class UexTerminalDistance(BaseModel):
    """Public result of `UexClient.get_terminal_distance()`.

    Combines the raw `/terminals_distances` payload with the two terminal
    IDs that were actually requested, since the API itself never echoes
    them back -- only names/codes.
    """

    model_config = ConfigDict(extra="ignore")

    terminal_id_origin: int
    terminal_id_destination: int
    terminal_code_origin: str
    terminal_name_origin: str
    orbit_name_origin: str | None = None
    terminal_code_destination: str
    terminal_name_destination: str
    orbit_name_destination: str | None = None
    distance_gm: float

    @classmethod
    def from_api(
        cls, *, terminal_id_origin: int, terminal_id_destination: int, data: _UexTerminalDistanceData
    ) -> "UexTerminalDistance":
        return cls(
            terminal_id_origin=terminal_id_origin,
            terminal_id_destination=terminal_id_destination,
            terminal_code_origin=data.terminal_code_origin,
            terminal_name_origin=data.terminal_name_origin,
            orbit_name_origin=data.orbit_name_origin,
            terminal_code_destination=data.terminal_code_destination,
            terminal_name_destination=data.terminal_name_destination,
            orbit_name_destination=data.orbit_name_destination,
            distance_gm=data.distance,
        )


class _UexTerminalsListResponse(BaseModel):
    """Envelope for `GET /terminals`."""

    model_config = ConfigDict(extra="ignore")

    status: str
    data: list[UexTerminal] = Field(default_factory=list)
    message: str = ""


class _UexTerminalDistanceResponse(BaseModel):
    """Envelope for `GET /terminals_distances`.

    `data` is `False` (not `null`) for a self-pair or a nonexistent
    terminal ID -- see module docstring.
    """

    model_config = ConfigDict(extra="ignore")

    status: str
    data: _UexTerminalDistanceData | bool | None = None
    message: str = ""


class UexStarSystem(BaseModel):
    """One entry from `GET /star_systems`.

    `is_available` is the flag `list_available_star_system_ids()` filters
    on to derive which systems are currently playable/queryable via
    `orbits_distances`, straight from live data rather than a hardcoded
    literal set -- see module docstring.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    code: str
    is_available: int = 0
    is_available_live: int = 0
    is_visible: int = 0


class _UexStarSystemsResponse(BaseModel):
    """Envelope for `GET /star_systems`."""

    model_config = ConfigDict(extra="ignore")

    status: str
    data: list[UexStarSystem] = Field(default_factory=list)
    message: str = ""


class UexOrbitDistance(BaseModel):
    """One row from `GET /orbits_distances?id_star_system={id}`.

    `distance` (aliased here to `distance_gm`) is, like
    `_UexTerminalDistanceData.distance`, sent as a numeric JSON string by
    the live API and coerced to `float` by pydantic's lax validation.
    Distances are **directional** -- `(id_orbit_origin, id_orbit_destination)`
    is not guaranteed to equal the reverse pair's distance (observed on a
    small number of Stanton jump-point-adjacent orbit pairs; see module
    docstring). Callers must not assume symmetry.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id_star_system_origin: int
    id_star_system_destination: int
    id_orbit_origin: int
    id_orbit_destination: int
    orbit_origin_name: str | None = None
    orbit_destination_name: str | None = None
    star_system_name: str | None = None
    distance_gm: float = Field(alias="distance")


class _UexOrbitsDistancesResponse(BaseModel):
    """Envelope for `GET /orbits_distances`."""

    model_config = ConfigDict(extra="ignore")

    status: str
    data: list[UexOrbitDistance] = Field(default_factory=list)
    message: str = ""


# --- Errors -------------------------------------------------------------


class UexApiError(RuntimeError):
    """Base class for every error this client raises."""


class UexApiTransientError(UexApiError):
    """A retryable failure: connection error, timeout, 5xx, 429, or UEX's
    documented `"requests_limit_reached"` business-level throttle status.

    `retry_after`, when set (from a `Retry-After` response header on a
    429), tells the retry wait strategy to use that exact delay instead of
    the default exponential backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UexApiClientError(UexApiError):
    """A non-retryable failure: 4xx (other than 429), or an invalid body."""


class UexApiConfigError(UexApiError):
    """Raised when the client can't proceed due to missing configuration.

    Not currently raised by anything in this module (no endpoint used here
    requires a key), but reserved so a future auth wall has a specific,
    clear exception to raise rather than a generic client error -- see the
    "no key configured" handling in `UexClient.__init__`.
    """


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header's delay-seconds form.

    Not empirically observed from the live UEX API (no 429 was ever
    produced during exploration), mirrors `wiki_client._parse_retry_after`
    -- the HTTP-date form is ignored (falls back to exponential backoff)
    rather than raising.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _build_wait_strategy(settings: Settings):
    """Build a tenacity wait callable honoring `Retry-After` when present.

    Falls back to exponential backoff (capped at `http_timeout_seconds *
    4`, floored at 1s) for every other transient failure. Identical policy
    to `wiki_client._build_wait_strategy` -- both clients share
    `settings.http_timeout_seconds` / `settings.http_max_retries` as one
    policy, per CLAUDE.md.
    """
    exponential = wait_exponential(multiplier=1, min=1, max=max(settings.http_timeout_seconds * 4, 1.0))

    def _wait(retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, UexApiTransientError) and exc.retry_after is not None:
            return exc.retry_after
        return exponential(retry_state)

    return _wait


class UexClient:
    """Synchronous client for UEX Corp's public API 2.0.

    Sync, not async: like `WikiClient`, this feeds a straight-line,
    once-in-a-while batch ingestion job (`backend/ingest/refresh.py`, Task
    4), not a request-serving hot path.

    Timeout and retry policy are both sourced from `backend.config
    .get_settings()` (`http_timeout_seconds`, `http_max_retries`) -- the
    *same* settings fields `wiki_client.py` uses, so both clients share one
    HTTP policy rather than each hardcoding/duplicating their own, per
    CLAUDE.md.

    No API key is required for any endpoint this client calls (`terminals`,
    `terminals_distances`, `orbits_distances`, `star_systems`) -- confirmed
    empirically, see module docstring.
    If `settings.uex_api_key` is set anyway, it's sent as `Authorization:
    Bearer {key}` (UEX's documented general auth mechanism) on every
    request; if unset, no auth header is sent at all and requests proceed
    anonymously, which is the expected/working default today.
    """

    def __init__(self, *, client: httpx.Client | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        headers = {"Accept": "application/json"}
        if self._settings.uex_api_key:
            headers["Authorization"] = f"Bearer {self._settings.uex_api_key}"
        self._client = client or httpx.Client(
            base_url=self._settings.uex_api_base_url,
            timeout=self._settings.http_timeout_seconds,
            headers=headers,
        )
        # `http_max_retries` is read as "how many retries", i.e. attempts
        # *beyond* the first -- total attempts = 1 + http_max_retries.
        self._retrying = Retrying(
            stop=stop_after_attempt(self._settings.http_max_retries + 1),
            wait=_build_wait_strategy(self._settings),
            retry=retry_if_exception_type(UexApiTransientError),
            before_sleep=self._log_retry,
            reraise=True,
        )

    def _log_retry(self, retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "uex_client retry attempt=%d error=%s",
            retry_state.attempt_number,
            exc,
        )

    def close(self) -> None:
        """Close the underlying `httpx.Client`, if this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "UexClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- low-level request plumbing --------------------------------------

    def _perform_request(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """One raw HTTP GET + status/JSON validation, no retrying.

        Raises `UexApiTransientError` for connection errors, timeouts, 5xx,
        429, and the documented `"requests_limit_reached"` business-level
        throttle status (retryable by `self._retrying`); raises
        `UexApiClientError` for any other `>=400` status or a body that
        isn't valid JSON (not retried).
        """
        try:
            response = self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            logger.warning("uex_client request timed out url=%s", url)
            raise UexApiTransientError(f"timeout requesting {url}") from exc
        except httpx.TransportError as exc:
            logger.warning("uex_client transport error url=%s error=%s", url, exc)
            raise UexApiTransientError(f"transport error requesting {url}") from exc

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "uex_client rate limited (429) url=%s retry_after=%s", url, retry_after
            )
            raise UexApiTransientError(
                f"rate limited (429) requesting {url}", retry_after=retry_after
            )
        if 500 <= response.status_code < 600:
            logger.warning(
                "uex_client server error url=%s status=%d", url, response.status_code
            )
            raise UexApiTransientError(
                f"server error {response.status_code} requesting {url}"
            )
        if response.status_code >= 400:
            logger.error(
                "uex_client client error url=%s status=%d", url, response.status_code
            )
            raise UexApiClientError(
                f"client error {response.status_code} requesting {url}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            logger.error("uex_client invalid JSON body url=%s", url)
            raise UexApiClientError(f"invalid JSON body from {url}") from exc

        # Documented business-level throttle: UEX's docs don't specify what
        # HTTP status this ships with, so it's checked regardless of the
        # (already-successful) status code above -- see module docstring.
        if isinstance(body, dict) and body.get("status") == "requests_limit_reached":
            logger.warning("uex_client rate limited (requests_limit_reached) url=%s", url)
            raise UexApiTransientError(f"requests_limit_reached requesting {url}")

        return body

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """`_perform_request`, wrapped with the shared retry/backoff policy."""
        return self._retrying(self._perform_request, url, params)

    # --- public API --------------------------------------------------------

    def list_terminals(
        self,
        *,
        id_star_system: int | None = None,
        type: str | None = None,  # noqa: A002 - matches UEX's own query param name
    ) -> list[UexTerminal]:
        """Fetch terminals, optionally scoped by star system and/or type.

        `GET /terminals` is not paginated -- the whole (optionally scoped)
        result set comes back in a single response (826 terminals with no
        filters at all, as observed 2026-08-14).
        """
        params: dict[str, Any] = {}
        if id_star_system is not None:
            params["id_star_system"] = id_star_system
        if type is not None:
            params["type"] = type
        raw = self._get_json("/terminals", params=params or None)
        parsed = _UexTerminalsListResponse.model_validate(raw)
        logger.info(
            "uex_client list_terminals count=%d id_star_system=%s type=%s",
            len(parsed.data),
            id_star_system,
            type,
        )
        return parsed.data

    def get_terminal_distance(
        self, terminal_id_origin: int, terminal_id_destination: int
    ) -> UexTerminalDistance | None:
        """Fetch the distance (gigameters) between two terminals.

        Returns `None` -- not an error -- when UEX has no distance on
        record for this pair (`data: false` in the raw response; observed
        for self-pairs and for nonexistent terminal IDs). This is a
        single-pair lookup; there is no bulk/matrix form of this endpoint
        (see module docstring) -- computing a full distance matrix means
        calling this once per pair.
        """
        raw = self._get_json(
            "/terminals_distances",
            params={
                "id_terminal_origin": terminal_id_origin,
                "id_terminal_destination": terminal_id_destination,
            },
        )
        parsed = _UexTerminalDistanceResponse.model_validate(raw)
        if parsed.data is None or parsed.data is False:
            logger.info(
                "uex_client get_terminal_distance no_data origin=%d destination=%d",
                terminal_id_origin,
                terminal_id_destination,
            )
            return None
        return UexTerminalDistance.from_api(
            terminal_id_origin=terminal_id_origin,
            terminal_id_destination=terminal_id_destination,
            data=parsed.data,
        )

    def list_star_systems(self) -> list[UexStarSystem]:
        """Fetch every star system UEX knows about (live + not-yet-live)."""
        raw = self._get_json("/star_systems")
        parsed = _UexStarSystemsResponse.model_validate(raw)
        logger.info("uex_client list_star_systems count=%d", len(parsed.data))
        return parsed.data

    def list_available_star_system_ids(self) -> list[int]:
        """Derive which star systems are currently playable/available.

        Sourced from live data (`is_available == 1` on `/star_systems`,
        currently Nyx/Pyro/Stanton) rather than a hardcoded literal set, so
        a new system going live in-game is picked up automatically on the
        next call -- no code change required. See module docstring for the
        empirical confirmation this flag is the right one to filter on.
        """
        systems = self.list_star_systems()
        ids = [system.id for system in systems if system.is_available == 1]
        logger.info("uex_client list_available_star_system_ids count=%d ids=%s", len(ids), ids)
        return ids

    def list_orbits_distances(self, star_system_id: int) -> list[UexOrbitDistance]:
        """Fetch every orbit-pair distance within one star system.

        Unlike `terminals_distances`, this *is* a bulk endpoint -- one call
        returns every orbit pair for the given system (observed: 1548 rows
        for Stanton, 1724 for Pyro, 246 for Nyx). This is the building
        block for bulk distance ingestion; see `iter_all_orbit_distances`
        and the module docstring's "Bulk distance ingestion" section.
        """
        raw = self._get_json("/orbits_distances", params={"id_star_system": star_system_id})
        parsed = _UexOrbitsDistancesResponse.model_validate(raw)
        logger.info(
            "uex_client list_orbits_distances star_system_id=%d count=%d",
            star_system_id,
            len(parsed.data),
        )
        return parsed.data

    def iter_all_orbit_distances(self) -> Iterator[UexOrbitDistance]:
        """Yield every orbit-pair distance across every currently
        available/playable star system.

        This is the recommended bulk-ingestion path: one `/star_systems`
        call to discover which systems are live (`list_available_star_
        system_ids`), then one `/orbits_distances` call per system -- 4
        total requests as of this writing for orbit-level distance
        coverage of the whole game, versus up to 12,880 individual
        `terminals_distances` calls for the equivalent terminal-pair
        coverage. See module docstring for the coverage verification
        (zero gaps across all 3 currently-available systems).
        """
        system_ids = self.list_available_star_system_ids()
        total = 0
        for system_id in system_ids:
            rows = self.list_orbits_distances(system_id)
            total += len(rows)
            yield from rows
        logger.info(
            "uex_client iter_all_orbit_distances systems=%d total_rows=%d",
            len(system_ids),
            total,
        )

    def list_all_orbit_distances(self) -> list[UexOrbitDistance]:
        """Eagerly collect `iter_all_orbit_distances()` into a list."""
        return list(self.iter_all_orbit_distances())
