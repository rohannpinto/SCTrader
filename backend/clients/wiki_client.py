"""Client for `api.star-citizen.wiki`'s commodities/prices endpoints.

Verified empirically against the live API (2026-08-14) -- see
`tests/fixtures/wiki_*.json` for real, unmodified captured responses and
`tests/unit/test_wiki_client.py` for the respx-mocked test suite built on
top of them.

Confirmed shape
----------------
- Base URL: ``https://api.star-citizen.wiki/api`` (i.e.
  `settings.wiki_api_base_url` as already configured in `backend/config.py`
  -- `GET {base}/commodities` returns `200` with a real payload, so no
  change was needed there).
- ``GET /commodities`` returns a Laravel-style paginated envelope:
  ``{"data": [...], "links": {"first", "last", "prev", "next"}, "meta":
  {"current_page", "last_page", "per_page", "total", ...}}``. Page size is
  configurable via the (bracketed) query param ``page[size]`` -- the server
  clamps it silently, observed default `30`, observed max `200` (a
  requested `1000` came back as `200`). Page number is `page[number]`, and
  is also embedded pre-built in `links.next`, which this client follows
  verbatim rather than reconstructing query params itself.
- List items (`data[]` on the list endpoint) do **not** include prices --
  only identity/catalog fields. Prices only appear on the **detail**
  endpoint, ``GET /commodities/{slug}``, whose payload is
  ``{"data": {...full commodity..., "uex_prices": {"purchase": [...]}}}``.
  Each `purchase[]` entry is one terminal's current buy/sell/rent price for
  that commodity, sourced from UEX, with a nested `starmap_location`.
- A non-existent slug returns HTTP `404` with an **HTML** error page (not
  JSON) -- `_perform_request` below treats any `>=400` status as an error
  before ever attempting `response.json()`, so this can't crash the client
  with a JSON-decode error instead of a clean `WikiApiClientError`.

Caching headers (documented, not yet acted on)
-----------------------------------------------
Every response observed carried ``Cache-Control: private, max-age=43200``
(12h) and ``Last-Modified``, served through Cloudflare (``cf-cache-status``,
``Age``). **No `ETag` header was ever observed** on either the list or
detail endpoint, on any request made during exploration. A future
conditional-request optimization could use `If-Modified-Since` /
`Last-Modified` (not `If-None-Match` / `ETag`, since none is sent), but that
is out of scope for this task -- noted here only per the task's "just note
whether it'd be possible later" ask.

Rate limits (documented, not yet acted on)
-------------------------------------------
No `X-RateLimit-*` (or any other rate-limit) response headers were observed
across dozens of requests during exploration (including a burst of
sequential requests to the same detail endpoint), and no public API docs
endpoint was discoverable (`/api/docs`, `/openapi.json`,
`/api/documentation` all `404`). Undocumented/invisible rate limiting is
still assumed possible in production use (hence the 429 handling below,
which respects `Retry-After` when a server ever does send one), but nothing
concrete was observed to size a proactive client-side limiter around.

Vehicles listing (Task 11)
---------------------------
Verified empirically against the live API (2026-08-15) -- see
`tests/fixtures/wiki_vehicle*.json` / `wiki_vehicles_list_page1.json` for
real, unmodified captures and CLAUDE.md's "Data source implementation
notes" addendum for the full write-up. Summary:

- ``GET /vehicles`` uses the exact same Laravel-style paginated envelope as
  `/commodities` (`data`/`links.next`/`meta.current_page`/`meta.last_page`),
  same silent page-size clamp (observed max `200`; a requested `1000` came
  back as `200`), same absolute-`http://`-URL-in-`links.next` quirk that
  needs `follow_redirects=True` (already set on this client's shared
  `httpx.Client` -- see "Confirmed shape" above). 295 total vehicles across
  2 pages at the clamped size, as of the capture date.
- **Unlike `/commodities`, list items on `/vehicles` are already the FULL
  vehicle record** -- not an identity-only stub requiring a separate
  detail call. Confirmed across all 295 items collected via pagination:
  every list item carries `quantum`, `cargo_capacity`, `manufacturer`, and
  everything else a detail call (`GET /vehicles/{slug}`, which does exist
  and returns the identical shape wrapped the same way) would also return.
  Consequently there is **no `get_vehicle_detail`-equivalent method** on
  this client -- `iter_vehicles`/`list_all_vehicles` already return
  everything ingestion needs in one bulk-listing pass, actually *fewer*
  requests than the commodities pipeline needs (which requires one detail
  call per commodity).
- **`is_spaceship` reliably separates real player spaceships from
  everything else.** Of 295 total vehicles, 247 have `is_spaceship: true`
  and 48 have `is_spaceship: false`. Every one of the 48 non-spaceships
  (ground vehicles like the Ballista/Centurion/Spartan, gravlev vehicles
  like the Nox, and power-suit "vehicles" like the ATLS) has
  `quantum.quantum_range: null` -- confirmed to be `null` for all 48, no
  exceptions -- so there's no risk of a ground vehicle sneaking into the
  ship table via the range check alone; `is_spaceship` is still checked
  explicitly first for clarity/defense in depth.
- **`quantum` is never `null` itself on a spaceship, but
  `quantum.quantum_range` inside it can be.** Of the 247 real spaceships,
  10 have `quantum.quantum_range: null` -- small craft with no quantum
  drive at all (`MPUV Cargo`, `MPUV Personnel`, `MPUV Tractor`, `Pitbull`,
  `P-52 Merlin`, `P-72 Archimedes` and its `Emerald` variant, `Fury` and
  its `LX`/`MX` variants). These must be skipped, not crashed on or
  defaulted to `0.0` (a real `0.0` range would be indistinguishable from
  "no drive" and would corrupt route search). 237 of 247 spaceships
  (96%) have real, usable `quantum_range` data.
- **`cargo_capacity` was never observed `null` for a real spaceship** (0 of
  247), but is frequently a real, present `0` (117 of 247, mostly pure
  fighters/interceptors with no cargo grid) -- a genuine, intentional
  value, not a gap, so a present `0` is kept as `0.0`, never treated as
  missing. A genuinely-`null` `cargo_capacity`, if one ever appeared, is
  **skipped** by ingestion (`_reconcile_ships` in `backend/ingest/
  refresh.py`), with a logged warning, the same way a `null`
  `quantum_range` is skipped -- not silently coerced to `0.0`, which would
  make it indistinguishable from a real zero-cargo fighter. The
  `Ship.cargo_capacity_scu` DB column is still declared `not null` per the
  task spec since a row is only ever written for a ship that had a real,
  present value.
- **`manufacturer` was never observed `null`** for any of the 237 usable
  spaceships; `manufacturer.name` is what CLAUDE.md/Task 11 call
  `manufacturer_name`. Still modeled as optional (`WikiVehicleManufacturer
  | None`) since the field is nullable in the schema and a defensive
  `None`-check costs nothing.
- **Unit of `quantum_range` confirmed meters.** Cross-checked the
  Caterpillar (`quantum_range: 70284406669`, i.e. `70.284406669` after
  `/ 1e9`) against Star Citizen's publicly known ~70 million km spec sheet
  figure -- matches once treated as meters -> gigameters, consistent with
  the existing `Distance` table's gigameter unit. Sanity range across all
  237 usable spaceships: min `41.74` Gm (small fighters like the `L-22
  Alpha Wolf`) to max `10204.08` Gm (the `F8A Lightning`, an outlier by
  design -- capital ships like the `Idris-P` sit around `1957` Gm,
  `Mauler Destroyer` around `3263` Gm). All strictly positive, all the
  same unit, consistent with meters at Star Citizen's in-lore scale.

Null vs. zero prices
---------------------
Per CLAUDE.md's edge-weight formula, a *missing* price must never be
coerced to `0.0` -- `0.0` means "known and unprofitable", missing means
"not traded there at all". Empirically, every `purchase[]` entry sampled
across all 206 live commodities used an explicit numeric `0` for "not
offered at this terminal" (e.g. `price_buy: 0` at a sell-only terminal),
never a JSON `null`, for `price_buy`/`price_sell` specifically -- but
`price_rent` **does** come back as `null` routinely, proving the API is
willing to send `null` for an absent price field. `WikiPriceEntry` below
declares `price_buy`/`price_sell`/`price_rent` as `float | None` and never
defaults or coerces a missing/`null` value to `0.0`, so the distinction
survives parsing intact regardless of which fields the live API sends as
`null` today or in the future. (Whether a given commodity is even present
at a given terminal *at all* -- the other flavor of "missing" -- is a
separate, coarser-grained fact: a terminal simply has no entry in that
commodity's `purchase[]` list. Reconciling that against the full terminal
set is Task 4's job, not this client's.)
"""

from __future__ import annotations

import logging
from datetime import datetime
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
# These describe the *wiki API's* response shape, deliberately kept separate
# from `backend/models/db.py` / `backend/models/schemas.py` (our internal
# contract) -- Task 4's ingestion pipeline is what translates between them.
# Every model below uses `extra="ignore"` so additions to the live schema
# (new fields the wiki API starts sending) never break parsing; only the
# fields this app actually needs are declared.


class WikiStarmapLocation(BaseModel):
    """The `starmap_location` object nested in each `WikiPriceEntry`."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    name: str
    slug: str
    type_name: str | None = None
    parent_name: str | None = None
    star_system_name: str | None = None
    link: str | None = None
    web_url: str | None = None


class WikiPriceEntry(BaseModel):
    """One terminal's current buy/sell/rent price for a commodity.

    `price_buy` / `price_sell` / `price_rent` are `float | None` on purpose
    -- see the module docstring's "Null vs. zero prices" section. Never
    coerce a missing/`None` value here to `0.0`.
    """

    model_config = ConfigDict(extra="ignore")

    price_buy: float | None = None
    price_sell: float | None = None
    price_rent: float | None = None
    terminal_id: int
    terminal_code: str | None = None
    terminal_name: str
    starmap_location: WikiStarmapLocation | None = None
    date_updated: datetime | None = None
    game_version: str | None = None


class WikiUexPrices(BaseModel):
    """The `uex_prices` object on a commodity detail response."""

    model_config = ConfigDict(extra="ignore")

    purchase: list[WikiPriceEntry] = Field(default_factory=list)


class WikiCommodityListItem(BaseModel):
    """One entry in the paginated `GET /commodities` list. No price data."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    slug: str
    name: str
    key: str | None = None


class WikiCommodityDetail(BaseModel):
    """Full `GET /commodities/{slug}` response payload (the fields we use)."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    slug: str
    name: str
    key: str | None = None
    uex_prices: WikiUexPrices = Field(default_factory=WikiUexPrices)

    @property
    def purchase_entries(self) -> list[WikiPriceEntry]:
        """Convenience accessor for `uex_prices.purchase`."""
        return self.uex_prices.purchase


class _WikiListLinks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    next: str | None = None


class _WikiListMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current_page: int
    last_page: int


class _WikiCommodityListResponse(BaseModel):
    """Envelope for `GET /commodities` -- only the fields the client uses."""

    model_config = ConfigDict(extra="ignore")

    data: list[WikiCommodityListItem]
    links: _WikiListLinks
    meta: _WikiListMeta


class _WikiCommodityDetailResponse(BaseModel):
    """Envelope for `GET /commodities/{slug}`."""

    model_config = ConfigDict(extra="ignore")

    data: WikiCommodityDetail


class WikiVehicleManufacturer(BaseModel):
    """The `manufacturer` object nested in a `WikiVehicleListItem`."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    code: str | None = None


class WikiVehicleQuantum(BaseModel):
    """The `quantum` object nested in a `WikiVehicleListItem`.

    `quantum_range` is `float | None` on purpose: a real spaceship can
    genuinely have no quantum drive at all (`quantum_range: null`) -- see
    module docstring's "Vehicles listing" section. Never defaulted to
    `0.0`, which would be indistinguishable from a real zero-range reading.
    """

    model_config = ConfigDict(extra="ignore")

    quantum_range: float | None = None


class WikiVehicleListItem(BaseModel):
    """One entry in the paginated `GET /vehicles` list.

    Despite the name (mirroring `WikiCommodityListItem` for this module's
    naming convention), this *is* the full vehicle record -- `/vehicles`
    list items are not identity-only stubs the way `/commodities` list
    items are. See module docstring's "Vehicles listing" section: there is
    deliberately no separate `get_vehicle_detail`-equivalent method on this
    client, because nothing it would return isn't already here.
    """

    model_config = ConfigDict(extra="ignore")

    uuid: str
    name: str
    is_spaceship: bool = False
    manufacturer: WikiVehicleManufacturer | None = None
    quantum: WikiVehicleQuantum | None = None
    cargo_capacity: float | None = None

    @property
    def manufacturer_name(self) -> str | None:
        """Convenience accessor for `manufacturer.name`."""
        return self.manufacturer.name if self.manufacturer is not None else None

    @property
    def quantum_range_m(self) -> float | None:
        """Convenience accessor for `quantum.quantum_range` (meters, or
        `None` if this vehicle has no quantum drive / quantum data at all).
        """
        return self.quantum.quantum_range if self.quantum is not None else None


class _WikiVehicleListResponse(BaseModel):
    """Envelope for `GET /vehicles` -- identical shape to `_WikiCommodityListResponse`."""

    model_config = ConfigDict(extra="ignore")

    data: list[WikiVehicleListItem]
    links: _WikiListLinks
    meta: _WikiListMeta


# --- Errors -------------------------------------------------------------


class WikiApiError(RuntimeError):
    """Base class for every error this client raises."""


class WikiApiTransientError(WikiApiError):
    """A retryable failure: connection error, timeout, 5xx, or 429.

    `retry_after`, when set (from a `Retry-After` response header on a 429),
    tells the retry wait strategy to use that exact delay instead of the
    default exponential backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class WikiApiClientError(WikiApiError):
    """A non-retryable failure: 4xx (other than 429), or an invalid body."""


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header's delay-seconds form.

    The wiki API has not been observed to send `Retry-After` at all (no 429
    was ever produced during exploration), and the HTTP spec also allows an
    HTTP-date form -- that form is simply ignored (falls back to exponential
    backoff) rather than raising, since a malformed/unsupported header must
    never crash the retry loop.
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
    4`, floored at 1s) for every other transient failure.
    """
    exponential = wait_exponential(multiplier=1, min=1, max=max(settings.http_timeout_seconds * 4, 1.0))

    def _wait(retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, WikiApiTransientError) and exc.retry_after is not None:
            return exc.retry_after
        return exponential(retry_state)

    return _wait


class WikiClient:
    """Synchronous client for `api.star-citizen.wiki`'s commodity endpoints.

    Sync, not async: the ingestion pipeline this feeds (`backend/ingest/
    refresh.py`, Task 4) is a straight-line, once-in-a-while batch job, not
    a request-serving hot path -- an `httpx.Client` keeps this simple with
    no clear concurrency win to justify the added complexity.

    Timeout and retry policy are both sourced from `backend.config
    .get_settings()` (`http_timeout_seconds`, `http_max_retries`) rather
    than hardcoded, so `backend/clients/uex_client.py` (Task 3) can share
    the exact same policy.
    """

    def __init__(self, *, client: httpx.Client | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._settings.wiki_api_base_url,
            timeout=self._settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
            # The wiki API's own `links.next` pagination field returns an
            # absolute `http://` URL for at least one real page (observed on
            # `/commodities` page 2) even though the API itself is served
            # over `https://`; requesting that literal URL gets a `301`
            # redirect (via Cloudflare) to the `https://` version. httpx
            # defaults `follow_redirects` to `False` (unlike `requests`),
            # so without this the client would receive the redirect's HTML
            # body and fail trying to parse it as JSON. Safe to follow
            # blindly here: this client only ever issues GET requests
            # against a fixed, hardcoded, trusted base URL -- never a
            # user-supplied URL -- so there's no open-redirect/SSRF concern
            # from doing so.
            follow_redirects=True,
        )
        # `http_max_retries` is read as "how many retries", i.e. attempts
        # *beyond* the first -- total attempts = 1 + http_max_retries.
        self._retrying = Retrying(
            stop=stop_after_attempt(self._settings.http_max_retries + 1),
            wait=_build_wait_strategy(self._settings),
            retry=retry_if_exception_type(WikiApiTransientError),
            before_sleep=self._log_retry,
            reraise=True,
        )

    def _log_retry(self, retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "wiki_client retry attempt=%d error=%s",
            retry_state.attempt_number,
            exc,
        )

    def close(self) -> None:
        """Close the underlying `httpx.Client`, if this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WikiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- low-level request plumbing --------------------------------------

    def _perform_request(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """One raw HTTP GET + status/JSON validation, no retrying.

        Raises `WikiApiTransientError` for connection errors, timeouts,
        5xx, and 429 (retryable by `self._retrying`); raises
        `WikiApiClientError` for any other `>=400` status or a body that
        isn't valid JSON (not retried -- retrying a 404 or a malformed body
        would never succeed).
        """
        try:
            response = self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            logger.warning("wiki_client request timed out url=%s", url)
            raise WikiApiTransientError(f"timeout requesting {url}") from exc
        except httpx.TransportError as exc:
            logger.warning("wiki_client transport error url=%s error=%s", url, exc)
            raise WikiApiTransientError(f"transport error requesting {url}") from exc

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "wiki_client rate limited (429) url=%s retry_after=%s", url, retry_after
            )
            raise WikiApiTransientError(
                f"rate limited (429) requesting {url}", retry_after=retry_after
            )
        if 500 <= response.status_code < 600:
            logger.warning(
                "wiki_client server error url=%s status=%d", url, response.status_code
            )
            raise WikiApiTransientError(
                f"server error {response.status_code} requesting {url}"
            )
        if response.status_code >= 400:
            logger.error(
                "wiki_client client error url=%s status=%d", url, response.status_code
            )
            raise WikiApiClientError(
                f"client error {response.status_code} requesting {url}"
            )

        try:
            return response.json()
        except ValueError as exc:
            logger.error("wiki_client invalid JSON body url=%s", url)
            raise WikiApiClientError(f"invalid JSON body from {url}") from exc

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """`_perform_request`, wrapped with the shared retry/backoff policy."""
        return self._retrying(self._perform_request, url, params)

    # --- public API --------------------------------------------------------

    def iter_commodities(self, page_size: int = 200) -> Iterator[WikiCommodityListItem]:
        """Yield every commodity list item, transparently following pagination.

        `page_size` is sent as `page[size]`; the live API silently clamps
        it (observed max `200`). Subsequent pages are fetched by following
        `links.next` verbatim rather than reconstructing query params.
        """
        url: str | None = "/commodities"
        params: dict[str, Any] | None = {"page[size]": page_size}
        pages = 0
        while url is not None:
            raw = self._get_json(url, params=params)
            parsed = _WikiCommodityListResponse.model_validate(raw)
            pages += 1
            for item in parsed.data:
                yield item
            url = parsed.links.next
            params = None  # `next` already carries its own full query string

        logger.info("wiki_client listed commodities pages=%d", pages)

    def list_all_commodities(self, page_size: int = 200) -> list[WikiCommodityListItem]:
        """Eagerly collect every commodity across all pages into a list."""
        items = list(self.iter_commodities(page_size=page_size))
        logger.info("wiki_client list_all_commodities count=%d", len(items))
        return items

    def get_commodity_detail(self, slug: str) -> WikiCommodityDetail:
        """Fetch and parse the full detail (incl. prices) for one commodity."""
        raw = self._get_json(f"/commodities/{slug}")
        parsed = _WikiCommodityDetailResponse.model_validate(raw)
        return parsed.data

    def iter_vehicles(self, page_size: int = 200) -> Iterator[WikiVehicleListItem]:
        """Yield every vehicle list item, transparently following pagination.

        Same pagination convention as `iter_commodities` (`page[size]`,
        following `links.next` verbatim) -- see module docstring's
        "Vehicles listing" section for the confirmation. Unlike
        `iter_commodities`, each yielded item already carries everything
        (`quantum`, `cargo_capacity`, `manufacturer`, ...) -- no per-item
        detail call is needed.
        """
        url: str | None = "/vehicles"
        params: dict[str, Any] | None = {"page[size]": page_size}
        pages = 0
        while url is not None:
            raw = self._get_json(url, params=params)
            parsed = _WikiVehicleListResponse.model_validate(raw)
            pages += 1
            for item in parsed.data:
                yield item
            url = parsed.links.next
            params = None  # `next` already carries its own full query string

        logger.info("wiki_client listed vehicles pages=%d", pages)

    def list_all_vehicles(self, page_size: int = 200) -> list[WikiVehicleListItem]:
        """Eagerly collect every vehicle across all pages into a list."""
        items = list(self.iter_vehicles(page_size=page_size))
        logger.info("wiki_client list_all_vehicles count=%d", len(items))
        return items
