"""Ingestion pipeline: `wiki_client` + `uex_client` -> the local cache DB.

`run_refresh()` is the single public entrypoint, usable both from a manual
CLI script and from the (future) `POST /refresh` FastAPI route. It follows
four strict phases, in order, per CLAUDE.md and this task's design:

1. **Fetch everything into memory first** (`_fetch_all`) -- no DB writes.
   If any of the three fetch calls raises, nothing below ever runs and the
   on-disk cache is left completely untouched.
2. **Reconcile into internal rows, still in memory** (`_reconcile`) -- joins
   the wiki API's `terminal_id` directly against UEX's terminal `id` (Task
   3's confirmed clean join), builds the orbit-pair -> terminal-pair
   distance expansion, and applies the same-orbit divide-by-zero floor.
3. **Write everything in one transaction** (`_write_all`, via
   `session_scope()`) -- upsert `Terminal`/`Commodity` rows (stable internal
   `id`s across refreshes), replace-all `Price`/`Distance` rows (current
   state only, no history).
4. **`RefreshRun` bookkeeping** -- a `running` row is inserted and committed
   *before* phase 1 starts (so it's visible while the refresh is in
   flight), then updated to `success` or `failed` in its own small
   transaction once the outcome is known.

Failure handling: any exception raised during phases 1-3 is caught, the
`RefreshRun` row is marked `failed` with a (truncated) error message, and
`run_refresh` returns a `RefreshResult` with `status="failed"` rather than
re-raising -- callers (a CLI script, a future FastAPI route) check
`result.success`/`result.status` programmatically rather than needing to
catch an exception or parse logs. Because phase 3 only ever runs after
phases 1-2 have fully succeeded, and phase 3 itself writes through
`session_scope()` (commit-all-or-rollback-all), a failed refresh can never
leave the cache DB in a partially-written or worse state than before it
started.

Terminal-ID join and the "stub terminal" edge case
----------------------------------------------------
Per `uex_client.py`'s research (see CLAUDE.md), the wiki API's
`terminal_id`/`terminal_code` fields on each price entry are UEX's own
terminal `id`/`code`, verbatim -- so both `Terminal.wiki_terminal_id` and
`Terminal.uex_terminal_id` are always set to the *same* external id here.
Wiki and UEX aren't guaranteed to be perfectly in sync at every moment,
though: a price entry can reference a `terminal_id` that isn't in UEX's
`/terminals` response. Rather than dropping that price data, a minimal stub
`Terminal` row is created for it (`id_orbit=None`, best-effort name/location
from the price entry's own `terminal_name`/`starmap_location`), and a
warning is logged once per such terminal.

Distance expansion
--------------------
The `Distance` table is keyed on terminal pairs, but UEX's bulk distance
data (`orbits_distances`) is keyed on orbit pairs, and an orbit is coarser
than a terminal (many terminals can share one orbit). `_reconcile` builds an
in-memory `orbit_id -> [terminal external id]` map, restricted to
`is_commodity_trading=True` terminals (the only ones that matter for the
trade graph), then:

- For each orbit-pair distance row `(orbit_a, orbit_b, distance)`, emits one
  `Distance` row for every `(x, y)` with `x` in `orbit_a`'s terminal list and
  `y` in `orbit_b`'s -- propagated as-is, since orbit-pair distances are
  directional/asymmetric for a handful of real pairs (see `uex_client.py`).
- For every *ordered* pair of distinct commodity-trading terminals that
  share the same `id_orbit`, emits a `Distance` row floored at
  `settings.min_distance_floor` -- gives same-orbit pairs a sensible
  nonzero distance value, computed once here at ingestion time. (Phase 1
  used this to guard a divide-by-zero in the edge-weight formula; Phase 2
  removed that formula, but the same-orbit floor is still the right thing
  to store.)
- A terminal with `id_orbit is None` (unknown orbit, or the stub-terminal
  case above) simply never appears in the orbit map, so it gets no
  `Distance` rows at all -- expected and fine.

UEX's `id_orbit: 0` sentinel ("no parent orbit on record", e.g. terminal 422
"Admin - UEX Station") is normalized to `None` during reconciliation
(`_normalize_id_orbit`) so it behaves identically to a genuinely-unknown
orbit rather than being treated as a real orbit shared by every other
`id_orbit: 0` terminal.

Terminal location refinement (Task 12)
----------------------------------------
`Terminal.planet_name` / `.moon_name` / `.is_orbital_station` are additive
fields set alongside the existing `location_name` (`_pick_location_name`,
unchanged) in the same main UEX-terminal loop inside `_reconcile`, sourced
directly from `UexTerminal` fields already being fetched -- no new API
calls. `is_orbital_station = (space_station_name is not None)`. See
CLAUDE.md's Task 12 addendum for the empirical research on what fraction of
orbital stations actually have `planet_name`/`moon_name` populated (most
do; deep-space jump-point gateways and a handful of others do not). The
wiki-only stub-terminal path (no `UexTerminal` available) leaves these three
fields at their dataclass defaults (`None`/`None`/`False`) since there's no
source data to populate them from.

Ship ingestion (Task 11)
--------------------------
A genuinely separate phase from the terminal/commodity/price/distance
pipeline above -- its own fetch (`_fetch_ships`), reconcile
(`_reconcile_ships`), and write (`_upsert_ships`) functions, touching none
of `_reconcile`'s terminal-handling code. Source is
`WikiClient.list_all_vehicles()`; filter and rationale are documented in
that client's module docstring ("Vehicles listing") and CLAUDE.md's Task 11
addendum -- summary: keep only `is_spaceship=True` vehicles with both a
non-null `quantum.quantum_range` (meters, converted to gigameters via
`/ 1e9` here) and a non-null `cargo_capacity`, skipping and logging a
warning for every real spaceship missing either (quantum: a small minority
-- craft with no quantum drive at all, e.g. the P-52 Merlin; cargo: never
actually observed empirically, but handled the same way rather than
silently coerced to `0.0`, which would be indistinguishable from a real
zero-cargo fighter), upsert by `wiki_uuid` (same stable-internal-id pattern
as `Terminal`/`Commodity`).

Commodity curation (Phase 2 Task 13)
--------------------------------------
Not every entry the wiki API's `/commodities` catalog returns is a real,
repeatable trade good -- e.g. "Luminalia Gift" (a seasonal-event item) was
observed producing a nonsensical multi-million-aUEC "profitable" route in
Phase 1's real-data verification, because the edge-weight formula has no
way to know it isn't a material. `TRADEABLE_COMMODITY_GROUPS` (module level,
above) is an allowlist over the wiki API's own `commodity_groups` tag on
each commodity detail response; `_is_tradeable_commodity` applies it with OR
semantics (any one matching group is enough) inside `_reconcile`'s main
commodity loop, *before* a commodity or any of its prices are added to
`result.commodities`/`result.prices` -- so an excluded commodity never
reaches the DB at all, and never triggers stub-terminal creation for a
terminal that would otherwise only be referenced by its (excluded) price
data. Verified against the real, live catalog (206 commodities as of
2026-08-15): 157 pass, 49 excluded -- full breakdown, sampled items from
every included/excluded group, and the live re-confirmation that Luminalia
Gift is still excluded live in CLAUDE.md's Task 13 addendum.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Engine, delete, select

from backend.clients.uex_client import UexClient, UexOrbitDistance, UexTerminal
from backend.clients.wiki_client import WikiClient, WikiCommodityDetail, WikiVehicleListItem
from backend.config import Settings, get_settings
from backend.models.db import (
    Commodity,
    Distance,
    Price,
    RefreshRun,
    Ship,
    Terminal,
    session_scope,
)

logger = logging.getLogger(__name__)

#: `RefreshRun.error_message` is truncated to this length before storage, so
#: a pathological exception message can never bloat the cache DB unbounded.
_ERROR_MESSAGE_MAX_LENGTH = 2000

#: Commodity curation (Phase 2 Task 13). A commodity is only written to the
#: `Commodity`/`Price` tables if at least one of its `commodity_groups`
#: values (OR semantics -- not all groups need to match) is in this set.
#: These are the wiki API's `commodity_groups` tags that correspond to real,
#: legitimately-tradeable materials/goods (raw ores, refined
#: metals/minerals/gases, synthetic materials, and consumables/vice items
#: players actually run repeatable trade routes with). Everything else
#: observed live -- `ProcessedGoods` (a mixed bag: some real refined goods,
#: but also non-commodities like ship-loot cosmetics), `Bulk_Supplies` (ship
#: ammo/countermeasures), `HeatPlaceholder`/`PowerPlaceholder`/
#: `LifeSupportPlaceholder`/`CleanAir` (engine placeholders, not real trade
#: goods) -- stays excluded. This is what keeps a route search from
#: "profitably" trading a seasonal collectible like Luminalia Gift. Full
#: empirical justification (live pass/exclude counts, sampled items from
#: each excluded/included group) lives in CLAUDE.md's Task 13 addendum --
#: not duplicated here.
TRADEABLE_COMMODITY_GROUPS = frozenset(
    {
        "Metal",
        "Mineral",
        "Nonmetal",
        "Halogen",
        "Alloy",
        "Gas",
        "UnrefinedOres",
        "Raw_Minerals",
        "SyntheticMaterials",
        "Waste",
        "Food",
        "Organic",
        "Vice",
    }
)


def _is_tradeable_commodity(detail: WikiCommodityDetail) -> bool:
    """OR-semantics allowlist check: qualifies if *any* group matches."""
    return not TRADEABLE_COMMODITY_GROUPS.isdisjoint(detail.commodity_groups)


# --- public result type -----------------------------------------------------


@dataclass(frozen=True)
class RefreshResult:
    """Programmatic, exception-free outcome of a `run_refresh()` call.

    Callers (a CLI script, a future FastAPI route) branch on `status`/
    `success` rather than needing to catch an exception or parse logs --
    see the module docstring's "Failure handling" section.
    """

    run_id: int
    status: str  # "success" or "failed" -- mirrors `RefreshRun.status`
    commodities_count: Optional[int] = None
    terminals_count: Optional[int] = None
    prices_count: Optional[int] = None
    distances_count: Optional[int] = None
    ships_count: Optional[int] = None
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "success"


# --- phase 1: fetch (no DB access) ------------------------------------------


@dataclass
class _FetchedData:
    uex_terminals: list[UexTerminal]
    commodity_details: list[WikiCommodityDetail]
    orbit_distances: list[UexOrbitDistance]
    vehicles: list[WikiVehicleListItem]


def _fetch_ships(wiki: WikiClient) -> list[WikiVehicleListItem]:
    """Fetch every vehicle list item. Separate function (not inlined into
    `_fetch_all`) so ship fetching stays its own isolated step, mirroring
    how `_reconcile_ships`/`_upsert_ships` stay isolated in the later
    phases -- see module docstring's "Ship ingestion" section.
    """
    vehicles = wiki.list_all_vehicles()
    logger.info("refresh fetch: vehicles=%d", len(vehicles))
    return vehicles


def _fetch_all(wiki: WikiClient, uex: UexClient) -> _FetchedData:
    """Phase 1: fetch every input this refresh needs, with zero DB writes.

    If any of the fetch steps raises, the exception propagates straight out
    -- the caller (`run_refresh`) never reaches phase 3, so the on-disk
    cache is left completely untouched.
    """
    uex_terminals = uex.list_terminals()
    commodity_type_counts = sum(1 for t in uex_terminals if t.type == "commodity")
    logger.info(
        "refresh fetch: terminals total=%d commodity=%d", len(uex_terminals), commodity_type_counts
    )

    commodity_list_items = wiki.list_all_commodities()
    commodity_details = [wiki.get_commodity_detail(item.slug) for item in commodity_list_items]
    logger.info("refresh fetch: commodities=%d", len(commodity_details))

    orbit_distances = uex.list_all_orbit_distances()
    logger.info("refresh fetch: orbit_distance_rows=%d", len(orbit_distances))

    vehicles = _fetch_ships(wiki)

    return _FetchedData(
        uex_terminals=uex_terminals,
        commodity_details=commodity_details,
        orbit_distances=orbit_distances,
        vehicles=vehicles,
    )


# --- phase 2: reconcile into internal rows (still in memory) ---------------


@dataclass
class _TerminalData:
    """Desired state for one `Terminal` row, keyed by external id upstream.

    `planet_name`/`moon_name`/`is_orbital_station` (Task 12) default to
    `None`/`None`/`False` so the wiki-only stub-terminal creation path below
    (which has no `UexTerminal` to source them from) doesn't need to be
    touched -- only the main UEX-terminal loop, which calls
    `_pick_location_name`, sets them explicitly.
    """

    name: str
    code: Optional[str]
    id_orbit: Optional[int]
    is_commodity_trading: bool
    raw_type: Optional[str]
    star_system_name: Optional[str]
    location_name: Optional[str]
    planet_name: Optional[str] = None
    moon_name: Optional[str] = None
    is_orbital_station: bool = False


@dataclass
class _CommodityData:
    """Desired state for one `Commodity` row, keyed by `wiki_uuid` upstream."""

    slug: str
    name: str


@dataclass
class _PriceData:
    """Desired state for one `Price` row, keyed by (terminal ext id, wiki_uuid)."""

    price_buy: Optional[float]
    price_sell: Optional[float]
    source_date_updated: Optional[datetime]


@dataclass
class _ShipData:
    """Desired state for one `Ship` row, keyed by `wiki_uuid` upstream."""

    name: str
    manufacturer_name: Optional[str]
    quantum_range_gm: float
    cargo_capacity_scu: float


@dataclass
class _ReconciledData:
    terminals: dict[int, _TerminalData] = field(default_factory=dict)
    commodities: dict[str, _CommodityData] = field(default_factory=dict)
    prices: dict[tuple[int, str], _PriceData] = field(default_factory=dict)
    # (terminal_a_ext_id, terminal_b_ext_id) -> distance
    distances: dict[tuple[int, int], float] = field(default_factory=dict)
    ships: dict[str, _ShipData] = field(default_factory=dict)


def _normalize_id_orbit(raw_id_orbit: Optional[int]) -> Optional[int]:
    """`0` is UEX's documented sentinel for "no parent orbit on record".

    Normalized to `None` so it's treated identically to a genuinely-unknown
    orbit (no `Distance` rows), rather than as a real orbit shared by every
    other `id_orbit: 0` terminal -- see module docstring.
    """
    if raw_id_orbit is None or raw_id_orbit == 0:
        return None
    return raw_id_orbit


def _pick_location_name(terminal: UexTerminal) -> Optional[str]:
    """Best-effort "parent body/station name" from the real `UexTerminal` fields.

    Most-specific first: a terminal's immediate parent is usually a space
    station, outpost, or city; falls back to progressively coarser bodies
    only when the more specific fields are unset.
    """
    for candidate in (
        terminal.space_station_name,
        terminal.outpost_name,
        terminal.city_name,
        terminal.moon_name,
        terminal.planet_name,
        terminal.orbit_name,
    ):
        if candidate:
            return candidate
    return None


#: `WikiVehicleListItem.quantum_range_m` is in meters; the `Ship` table (and
#: the existing `Distance` table) store gigameters.
_METERS_PER_GIGAMETER = 1e9


def _reconcile_ships(vehicles: list[WikiVehicleListItem]) -> dict[str, _ShipData]:
    """Filter + convert the raw vehicle list into desired `Ship` rows.

    Standalone function, deliberately separate from `_reconcile`'s
    terminal/commodity/price/distance logic -- see module docstring's "Ship
    ingestion" section. Keeps only real player spaceships with usable
    quantum-drive *and* cargo data (`is_spaceship=True`, `quantum_range_m`
    non-null, `cargo_capacity` non-null -- see `wiki_client.py`'s "Vehicles
    listing" docstring for the empirical basis), logging a warning for each
    real spaceship skipped for either reason. A real, present `0` for
    `cargo_capacity` (a pure fighter with no cargo grid -- common,
    empirically ~half of real ships) is kept as-is, not skipped: only an
    actually-missing (`None`) value is treated as unusable, same
    missing-vs-zero distinction CLAUDE.md's edge-weight formula draws for
    prices. Non-spaceships (ground vehicles, gravlev, power suits) are
    skipped silently -- that's the expected, routine case, not a
    data-quality problem worth a warning.
    """
    ships: dict[str, _ShipData] = {}
    skipped_missing_quantum = 0
    skipped_missing_cargo = 0

    for vehicle in vehicles:
        if not vehicle.is_spaceship:
            continue

        quantum_range_m = vehicle.quantum_range_m
        if quantum_range_m is None:
            skipped_missing_quantum += 1
            logger.warning(
                "refresh: skipping spaceship uuid=%s name=%s -- no usable quantum_range data",
                vehicle.uuid,
                vehicle.name,
            )
            continue

        if vehicle.cargo_capacity is None:
            skipped_missing_cargo += 1
            logger.warning(
                "refresh: skipping spaceship uuid=%s name=%s -- no usable cargo_capacity data",
                vehicle.uuid,
                vehicle.name,
            )
            continue

        ships[vehicle.uuid] = _ShipData(
            name=vehicle.name,
            manufacturer_name=vehicle.manufacturer_name,
            quantum_range_gm=quantum_range_m / _METERS_PER_GIGAMETER,
            cargo_capacity_scu=vehicle.cargo_capacity,
        )

    if skipped_missing_quantum:
        logger.warning(
            "refresh: skipped %d spaceship(s) with no usable quantum_range data",
            skipped_missing_quantum,
        )
    if skipped_missing_cargo:
        logger.warning(
            "refresh: skipped %d spaceship(s) with no usable cargo_capacity data",
            skipped_missing_cargo,
        )
    logger.info(
        "refresh reconcile: ships=%d (skipped_missing_quantum=%d, skipped_missing_cargo=%d, "
        "from %d total vehicles)",
        len(ships),
        skipped_missing_quantum,
        skipped_missing_cargo,
        len(vehicles),
    )
    return ships


def _reconcile(fetched: _FetchedData, settings: Settings) -> _ReconciledData:
    """Phase 2: join wiki + UEX data into desired internal rows, in memory.

    No DB access happens here -- everything is plain Python dicts, written
    to the database in one shot by `_write_all` (phase 3).
    """
    result = _ReconciledData()

    # --- terminals, from the full UEX terminals list (all 826, not just
    # commodity-typed) -----------------------------------------------------
    for uex_terminal in fetched.uex_terminals:
        result.terminals[uex_terminal.id] = _TerminalData(
            name=uex_terminal.name,
            code=uex_terminal.code,
            id_orbit=_normalize_id_orbit(uex_terminal.id_orbit),
            is_commodity_trading=(uex_terminal.type == "commodity"),
            raw_type=uex_terminal.type,
            star_system_name=uex_terminal.star_system_name,
            location_name=_pick_location_name(uex_terminal),
            planet_name=uex_terminal.planet_name,
            moon_name=uex_terminal.moon_name,
            is_orbital_station=uex_terminal.space_station_name is not None,
        )

    # --- commodities + prices, from wiki commodity details ------------------
    # Curated (Phase 2 Task 13): a commodity outside `TRADEABLE_COMMODITY_
    # GROUPS` is skipped entirely here -- never written to `result
    # .commodities`, and its `purchase_entries` are never walked, so it never
    # reaches `result.prices` either. This is what keeps non-material/
    # seasonal/placeholder items (e.g. "Luminalia Gift") out of the trade
    # graph at the source, rather than filtering them at every consumer.
    stub_terminal_ext_ids_seen: set[int] = set()
    skipped_commodity_count = 0
    for detail in fetched.commodity_details:
        if not _is_tradeable_commodity(detail):
            skipped_commodity_count += 1
            continue

        result.commodities[detail.uuid] = _CommodityData(slug=detail.slug, name=detail.name)

        for entry in detail.purchase_entries:
            ext_id = entry.terminal_id

            if ext_id not in result.terminals and ext_id not in stub_terminal_ext_ids_seen:
                stub_terminal_ext_ids_seen.add(ext_id)
                location = entry.starmap_location
                logger.warning(
                    "refresh: wiki terminal_id=%s (code=%s, name=%s) not present in UEX "
                    "terminals list; creating stub terminal to preserve its price data "
                    "(first seen on commodity=%s)",
                    ext_id,
                    entry.terminal_code,
                    entry.terminal_name,
                    detail.slug,
                )
                result.terminals[ext_id] = _TerminalData(
                    name=entry.terminal_name,
                    code=entry.terminal_code,
                    id_orbit=None,
                    is_commodity_trading=True,
                    raw_type=None,
                    star_system_name=location.star_system_name if location else None,
                    location_name=location.parent_name if location else None,
                )

            result.prices[(ext_id, detail.uuid)] = _PriceData(
                price_buy=entry.price_buy,
                price_sell=entry.price_sell,
                source_date_updated=entry.date_updated,
            )

    if stub_terminal_ext_ids_seen:
        logger.warning(
            "refresh: created %d stub terminal(s) for wiki terminal_ids absent from UEX",
            len(stub_terminal_ext_ids_seen),
        )
    logger.info(
        "refresh reconcile: commodities kept=%d skipped_by_allowlist=%d (of %d fetched)",
        len(result.commodities),
        skipped_commodity_count,
        len(fetched.commodity_details),
    )

    # --- distances: orbit-pair expansion + same-orbit floor -----------------
    orbit_to_terminal_ext_ids: dict[int, list[int]] = defaultdict(list)
    for ext_id, terminal_data in result.terminals.items():
        if terminal_data.is_commodity_trading and terminal_data.id_orbit is not None:
            orbit_to_terminal_ext_ids[terminal_data.id_orbit].append(ext_id)

    for row in fetched.orbit_distances:
        if row.id_orbit_origin == row.id_orbit_destination:
            continue  # same-orbit pairs are handled by the floor loop below
        origin_terminals = orbit_to_terminal_ext_ids.get(row.id_orbit_origin)
        destination_terminals = orbit_to_terminal_ext_ids.get(row.id_orbit_destination)
        if not origin_terminals or not destination_terminals:
            continue  # neither orbit has any commodity-trading terminal
        for x in origin_terminals:
            for y in destination_terminals:
                result.distances[(x, y)] = row.distance_gm

    floor = settings.min_distance_floor
    for terminal_ext_ids in orbit_to_terminal_ext_ids.values():
        for x in terminal_ext_ids:
            for y in terminal_ext_ids:
                if x != y:
                    result.distances[(x, y)] = floor

    # --- ships: a fully separate reconciliation step, see `_reconcile_ships` ---
    result.ships = _reconcile_ships(fetched.vehicles)

    logger.info(
        "refresh reconcile: terminals=%d (stubs=%d) commodities=%d prices=%d distances=%d ships=%d",
        len(result.terminals),
        len(stub_terminal_ext_ids_seen),
        len(result.commodities),
        len(result.prices),
        len(result.distances),
        len(result.ships),
    )
    return result


# --- phase 3: write everything in one transaction ---------------------------


@dataclass
class _WriteCounts:
    terminals: int
    commodities: int
    prices: int
    distances: int
    ships: int


def _upsert_terminals_and_commodities(
    session, reconciled: _ReconciledData
) -> tuple[dict[int, Terminal], dict[str, Commodity]]:
    """Upsert `Terminal`/`Commodity` rows, keeping internal `id`s stable.

    Looks up existing rows by external id (`Terminal`) / `wiki_uuid`
    (`Commodity`) and updates them in place when found, so nothing else
    that refers to a terminal/commodity by internal `id` needs to re-key
    across refreshes.
    """
    existing_terminal_rows: dict[int, Terminal] = {}
    for row in session.execute(select(Terminal)).scalars():
        key = row.uex_terminal_id if row.uex_terminal_id is not None else row.wiki_terminal_id
        if key is not None:
            existing_terminal_rows[key] = row

    terminal_rows_by_ext_id: dict[int, Terminal] = {}
    for ext_id, data in reconciled.terminals.items():
        row = existing_terminal_rows.get(ext_id)
        if row is None:
            row = Terminal(wiki_terminal_id=ext_id, uex_terminal_id=ext_id)
            session.add(row)
        row.name = data.name
        row.code = data.code
        row.id_orbit = data.id_orbit
        row.is_commodity_trading = data.is_commodity_trading
        row.raw_type = data.raw_type
        row.star_system_name = data.star_system_name
        row.location_name = data.location_name
        row.planet_name = data.planet_name
        row.moon_name = data.moon_name
        row.is_orbital_station = data.is_orbital_station
        terminal_rows_by_ext_id[ext_id] = row

    existing_commodity_rows: dict[str, Commodity] = {
        row.wiki_uuid: row for row in session.execute(select(Commodity)).scalars()
    }
    commodity_rows_by_uuid: dict[str, Commodity] = {}
    for wiki_uuid, data in reconciled.commodities.items():
        row = existing_commodity_rows.get(wiki_uuid)
        if row is None:
            row = Commodity(wiki_uuid=wiki_uuid, slug=data.slug, name=data.name)
            session.add(row)
        else:
            row.slug = data.slug
            row.name = data.name
        commodity_rows_by_uuid[wiki_uuid] = row

    # Flush (not commit) so newly-inserted rows get autoincrement PKs
    # assigned before Price/Distance rows below need to reference them.
    session.flush()
    return terminal_rows_by_ext_id, commodity_rows_by_uuid


def _upsert_ships(session, reconciled: _ReconciledData) -> int:
    """Upsert `Ship` rows by `wiki_uuid`, keeping internal `id`s stable.

    A fully separate write step from `_upsert_terminals_and_commodities`
    (not folded into it) -- see module docstring's "Ship ingestion"
    section. Same upsert-by-external-key shape as that function, applied to
    `Ship`/`wiki_uuid` instead of `Terminal`/`Commodity`.
    """
    existing_ship_rows: dict[str, Ship] = {
        row.wiki_uuid: row for row in session.execute(select(Ship)).scalars()
    }
    for wiki_uuid, data in reconciled.ships.items():
        row = existing_ship_rows.get(wiki_uuid)
        if row is None:
            row = Ship(wiki_uuid=wiki_uuid)
            session.add(row)
        row.name = data.name
        row.manufacturer_name = data.manufacturer_name
        row.quantum_range_gm = data.quantum_range_gm
        row.cargo_capacity_scu = data.cargo_capacity_scu

    return len(reconciled.ships)


def _replace_prices(
    session,
    reconciled: _ReconciledData,
    terminal_rows_by_ext_id: dict[int, Terminal],
    commodity_rows_by_uuid: dict[str, Commodity],
    fetched_at: datetime,
) -> int:
    """Delete every existing `Price` row and bulk-insert the fresh set.

    Prices reflect current state only (no history) -- a terminal that stops
    trading a commodity must have its old row disappear, not linger.
    """
    session.execute(delete(Price))
    rows = [
        Price(
            terminal_id=terminal_rows_by_ext_id[ext_id].id,
            commodity_id=commodity_rows_by_uuid[wiki_uuid].id,
            price_buy=data.price_buy,
            price_sell=data.price_sell,
            source_date_updated=data.source_date_updated,
            fetched_at=fetched_at,
        )
        for (ext_id, wiki_uuid), data in reconciled.prices.items()
    ]
    session.add_all(rows)
    return len(rows)


def _replace_distances(
    session,
    reconciled: _ReconciledData,
    terminal_rows_by_ext_id: dict[int, Terminal],
    fetched_at: datetime,
) -> int:
    """Delete every existing `Distance` row and bulk-insert the fresh set.

    Cheap to fully regenerate each time at this scale (~10k rows at most);
    same "current state only" reasoning as `_replace_prices`.
    """
    session.execute(delete(Distance))
    rows = [
        Distance(
            terminal_a_id=terminal_rows_by_ext_id[a].id,
            terminal_b_id=terminal_rows_by_ext_id[b].id,
            distance=distance,
            fetched_at=fetched_at,
        )
        for (a, b), distance in reconciled.distances.items()
    ]
    session.add_all(rows)
    return len(rows)


def _write_all(engine: Optional[Engine], reconciled: _ReconciledData, fetched_at: datetime) -> _WriteCounts:
    """Phase 3: upsert terminals/commodities/ships, replace-all prices/distances.

    All of it happens inside one `session_scope()` -- commits together, or
    (via `session_scope`'s existing rollback-on-exception behavior) none of
    it does. `_upsert_ships` is called alongside, not folded into,
    `_upsert_terminals_and_commodities` -- see module docstring's "Ship
    ingestion" section.
    """
    with session_scope(engine) as session:
        terminal_rows_by_ext_id, commodity_rows_by_uuid = _upsert_terminals_and_commodities(
            session, reconciled
        )
        prices_count = _replace_prices(
            session, reconciled, terminal_rows_by_ext_id, commodity_rows_by_uuid, fetched_at
        )
        distances_count = _replace_distances(session, reconciled, terminal_rows_by_ext_id, fetched_at)
        ships_count = _upsert_ships(session, reconciled)
        counts = _WriteCounts(
            terminals=len(terminal_rows_by_ext_id),
            commodities=len(commodity_rows_by_uuid),
            prices=prices_count,
            distances=distances_count,
            ships=ships_count,
        )

    logger.info(
        "refresh write: terminals=%d commodities=%d prices=%d distances=%d ships=%d",
        counts.terminals,
        counts.commodities,
        counts.prices,
        counts.distances,
        counts.ships,
    )
    return counts


# --- phase 4: RefreshRun bookkeeping + orchestration -------------------------


def _truncate_error_message(exc: Exception) -> str:
    message = str(exc)
    if len(message) > _ERROR_MESSAGE_MAX_LENGTH:
        message = message[:_ERROR_MESSAGE_MAX_LENGTH]
    return message


def run_refresh(
    *,
    engine: Optional[Engine] = None,
    wiki_client: Optional[WikiClient] = None,
    uex_client: Optional[UexClient] = None,
    settings: Optional[Settings] = None,
) -> RefreshResult:
    """Run one full ingestion refresh: fetch -> reconcile -> write -> record.

    Usable both from a CLI script (`scripts/refresh_data.py`) and from a
    future `POST /refresh` FastAPI route:

    - `engine`: target a specific SQLAlchemy `Engine` (e.g. a test's
      temp-file database via `session_scope(engine=...)`); `None` uses the
      process-wide default engine bound to `settings.db_path`.
    - `wiki_client` / `uex_client`: inject already-constructed clients (unit
      tests exercising the stub-terminal edge case sometimes find this
      easier than respx); `None` constructs fresh ones from `settings` and
      closes them again before returning.
    - `settings`: defaults to `backend.config.get_settings()`.

    Never raises for a failed *refresh* (a fetch error, a write error, etc.)
    -- those are caught, recorded on the `RefreshRun` row, and reported back
    via `RefreshResult.status`/`.success`/`.error_message`. This function
    can still raise for a genuinely unexpected bug in the bookkeeping itself
    (e.g. the DB being unreachable for the initial `RefreshRun` insert), by
    design -- there's no "started" row to report failure through in that
    case.
    """
    settings = settings or get_settings()
    started_at = datetime.now(timezone.utc)

    with session_scope(engine) as session:
        run = RefreshRun(status="running", started_at=started_at)
        session.add(run)
        session.flush()
        run_id = run.id
    logger.info("refresh run_id=%d started", run_id)

    owns_wiki_client = wiki_client is None
    owns_uex_client = uex_client is None
    wiki = wiki_client or WikiClient(settings=settings)
    uex = uex_client or UexClient(settings=settings)

    try:
        fetched = _fetch_all(wiki, uex)
        reconciled = _reconcile(fetched, settings)
        counts = _write_all(engine, reconciled, started_at)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any phase 1-3
        # failure must be recorded on the RefreshRun row and reported back
        # programmatically, never left as an unhandled crash.
        error_message = _truncate_error_message(exc)
        logger.exception("refresh run_id=%d failed", run_id)
        completed_at = datetime.now(timezone.utc)
        with session_scope(engine) as session:
            run = session.get(RefreshRun, run_id)
            run.status = "failed"
            run.completed_at = completed_at
            run.error_message = error_message
        return RefreshResult(run_id=run_id, status="failed", error_message=error_message)
    finally:
        if owns_wiki_client:
            wiki.close()
        if owns_uex_client:
            uex.close()

    completed_at = datetime.now(timezone.utc)
    with session_scope(engine) as session:
        run = session.get(RefreshRun, run_id)
        run.status = "success"
        run.completed_at = completed_at
        run.commodities_count = counts.commodities
        run.terminals_count = counts.terminals
        run.prices_count = counts.prices
        run.distances_count = counts.distances
        run.ships_count = counts.ships

    logger.info(
        "refresh run_id=%d succeeded terminals=%d commodities=%d prices=%d distances=%d ships=%d",
        run_id,
        counts.terminals,
        counts.commodities,
        counts.prices,
        counts.distances,
        counts.ships,
    )
    return RefreshResult(
        run_id=run_id,
        status="success",
        commodities_count=counts.commodities,
        terminals_count=counts.terminals,
        prices_count=counts.prices,
        distances_count=counts.distances,
        ships_count=counts.ships,
    )
