"""Pydantic v2 request/response models -- the API contract for the FastAPI
routers built in later tasks.

These models are intentionally decoupled from the SQLAlchemy models in
`backend/models/db.py`: `*Out` models expose only the fields a client needs
(and can be built from ORM rows via `model_validate(orm_obj)` thanks to
`from_attributes=True`), and request models add explicit validation bounds
per CLAUDE.md's security ground rules ("all user-facing input must be
validated server-side with explicit bounds").

Note: server-side caps (e.g. `max_hops_cap`, `max_starting_budget_cap` from
`backend.config.Settings`) and existence checks (e.g. "does this
`start_terminal_id`/`ship_id` actually exist") are enforced by the routers
that use these models, not here -- these models only enforce shape/type/sign
constraints that hold true regardless of runtime config.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CommodityOut(BaseModel):
    """A commodity as exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Internal Commodity.id surrogate key.")
    slug: str = Field(..., description="URL-safe unique commodity slug.")
    name: str = Field(..., description="Human-readable commodity name.")


class TerminalOut(BaseModel):
    """A terminal as exposed to API clients (e.g. the frontend's start-station selector)."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Internal Terminal.id surrogate key.")
    name: str = Field(..., description="Human-readable terminal name.")
    star_system_name: str | None = Field(
        default=None, description="Star system the terminal is located in, if known."
    )
    location_name: str | None = Field(
        default=None, description="Parent body/station name, if known."
    )
    planet_name: str | None = Field(
        default=None,
        description=(
            "Planet the terminal (or the moon/station it orbits) belongs to, if known. "
            "See CLAUDE.md's Task 12 addendum: most orbital stations resolve this "
            "correctly, but deep-space jump-point gateways and a handful of others "
            "have neither this nor `moon_name` set."
        ),
    )
    moon_name: str | None = Field(
        default=None, description="Moon the terminal orbits/sits on, if known."
    )
    is_orbital_station: bool = Field(
        default=False,
        description="True iff this terminal is an orbital station (not a ground terminal).",
    )


class ShipOut(BaseModel):
    """A real player spaceship as exposed to API clients (e.g. the
    frontend's future ship selector, Phase 2)."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Internal Ship.id surrogate key.")
    name: str = Field(..., description="Human-readable ship name.")
    manufacturer_name: str | None = Field(
        default=None, description="Manufacturer name, if known."
    )
    quantum_range_gm: float = Field(
        ..., description="Quantum drive range in gigameters."
    )
    cargo_capacity_scu: float = Field(
        ..., description="Cargo capacity in SCU (0 for a ship with no cargo grid)."
    )


class RouteRequest(BaseModel):
    """A request to search for a profitable trading route from a starting
    terminal (Phase 2: hop-count/cash/cargo model -- see CLAUDE.md's "Route
    search problem"). `max_distance`/`distance_threshold` no longer exist as
    concepts anywhere in this app; the selected ship's quantum range gates
    per-hop traversal instead (resolved server-side from `ship_id`, not a
    request field)."""

    start_terminal_id: int = Field(
        ..., description="Internal Terminal.id to start the route search from."
    )
    ship_id: int = Field(
        ...,
        description=(
            "Internal Ship.id of the ship to use for this search -- its "
            "quantum_range_gm gates per-hop traversal and its "
            "cargo_capacity_scu caps per-hop trade quantity."
        ),
    )
    num_hops: int = Field(
        ...,
        gt=0,
        description=(
            "Maximum number of hops the route search may take. Must be "
            "positive; also capped server-side at `settings.max_hops_cap`."
        ),
    )
    starting_budget: float = Field(
        ...,
        ge=0,
        description=(
            "Cash (aUEC) on hand at the start of the search. Must be "
            "non-negative; also capped server-side at "
            "`settings.max_starting_budget_cap`."
        ),
    )


class RouteHop(BaseModel):
    """A single leg of a found route: travel to a terminal and (optionally)
    trade a commodity there."""

    terminal_id: int = Field(..., description="Internal Terminal.id of this hop's destination.")
    terminal_name: str = Field(..., description="Human-readable name of this hop's destination.")
    commodity_id: int | None = Field(
        default=None,
        description=(
            "Internal Commodity.id of the commodity traded on this hop. `None` for a "
            "neutral \"bridge\" hop where no commodity was profitable given the cash on "
            "hand at this hop's origin (CLAUDE.md's Phase 2 search model: the edge still "
            "exists and can still be worth traversing to reach a profitable edge further "
            "along the route)."
        ),
    )
    commodity_name: str | None = Field(
        default=None,
        description="Human-readable name of the commodity traded on this hop, or `None` to match `commodity_id`.",
    )
    distance_from_previous: float = Field(
        ..., ge=0, description="In-game distance travelled from the previous hop (or start)."
    )
    quantity_traded: float = Field(
        ...,
        ge=0,
        description="Units of the commodity bought/sold on this hop (0 for a bridge hop).",
    )
    unit_buy_price: float | None = Field(
        default=None,
        description="Per-unit buy price at this hop's origin, or `None` for a bridge hop.",
    )
    unit_sell_price: float | None = Field(
        default=None,
        description="Per-unit sell price at this hop's destination, or `None` for a bridge hop.",
    )
    profit_this_hop: float = Field(
        ...,
        ge=0,
        description=(
            "Real, absolute aUEC profit realized on this hop "
            "(quantity_traded * (unit_sell_price - unit_buy_price)), 0.0 for a bridge hop."
        ),
    )


class RouteResponse(BaseModel):
    """Result of a route search.

    Explicitly and unambiguously represents both possible outcomes via the
    `found` flag:

    - `found=True`: `hops` is a non-empty ordered list of `RouteHop`,
      `final_cash` is strictly greater than `starting_budget` (CLAUDE.md's
      Phase 2 "found" definition -- real, positive realized profit, not
      merely "didn't lose money"), and `total_distance`/`total_profit`
      summarize the whole route.
    - `found=False`: no profitable route existed from `start_terminal_id`
      under the given constraints (including the "isolated start node, zero
      viable outgoing edges" case from CLAUDE.md). This is a normal,
      successful response -- not an error -- so `hops` is an empty list,
      `final_cash` equals `starting_budget`, `total_distance`/`total_profit`
      are `0.0`, and `message` carries a human-readable explanation for
      direct display.

    Callers should branch on `found`, never infer it from `hops` being
    empty (kept consistent by construction, but `found` is the contract).
    """

    found: bool = Field(..., description="Whether a profitable route was found.")
    start_terminal_id: int = Field(..., description="Echoes the requested starting terminal.")
    hops: list[RouteHop] = Field(
        default_factory=list, description="Ordered route legs; empty when `found` is False."
    )
    total_distance: float = Field(
        default=0.0,
        ge=0,
        description="Sum of `distance_from_previous` across all hops. Informational only -- no longer a search constraint.",
    )
    starting_budget: float = Field(
        ..., ge=0, description="Cash (aUEC) on hand at the start of the search -- echoes the request."
    )
    final_cash: float = Field(
        ...,
        ge=0,
        description=(
            "Cash (aUEC) on hand at the end of the best route found. Equals "
            "`starting_budget` when `found` is False."
        ),
    )
    total_profit: float = Field(
        default=0.0,
        ge=0,
        description="Net profit realized: `final_cash - starting_budget`.",
    )
    message: str | None = Field(
        default=None,
        description=(
            "Human-readable explanation, mainly used when `found` is False "
            "(e.g. \"no profitable route found from this start\")."
        ),
    )

    @model_validator(mode="after")
    def _check_found_hops_invariant(self) -> "RouteResponse":
        """Enforce `found=True` <=> non-empty `hops` at construction time.

        This used to be convention-only (see class docstring); enforcing it
        here means `RouteResponse(found=True, hops=[])` -- and the symmetric
        `found=False` with non-empty `hops` -- can no longer be constructed.
        """
        if self.found and not self.hops:
            raise ValueError("RouteResponse: `found=True` requires a non-empty `hops` list.")
        if not self.found and self.hops:
            raise ValueError("RouteResponse: `found=False` requires an empty `hops` list.")
        return self

    @model_validator(mode="after")
    def _check_total_profit_matches_cash_delta(self) -> "RouteResponse":
        """Enforce `total_profit == final_cash - starting_budget` (CLAUDE.md/
        Task 15: "total_profit is final_cash - starting_budget") -- the same
        "don't just trust convention, check it at construction time" spirit
        as the found/hops invariant above. `math.isclose` (not exact `==`)
        purely to tolerate ordinary floating-point summation noise, not to
        allow any real drift.
        """
        expected = self.final_cash - self.starting_budget
        if not math.isclose(self.total_profit, expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                "RouteResponse: `total_profit` must equal `final_cash - starting_budget` "
                f"(got total_profit={self.total_profit!r}, "
                f"final_cash - starting_budget={expected!r})."
            )
        return self


class RefreshStatusOut(BaseModel):
    """Status of the most recent (or in-progress) data refresh."""

    status: Literal["running", "success", "failed", "never_run"] = Field(
        ..., description="One of 'running', 'success', 'failed', or 'never_run'."
    )
    started_at: datetime | None = Field(default=None, description="When the refresh started.")
    completed_at: datetime | None = Field(
        default=None, description="When the refresh finished (success or failure)."
    )
    commodities_count: int | None = Field(default=None, ge=0)
    terminals_count: int | None = Field(default=None, ge=0)
    prices_count: int | None = Field(default=None, ge=0)
    distances_count: int | None = Field(default=None, ge=0)
    ships_count: int | None = Field(default=None, ge=0)
    error_message: str | None = Field(
        default=None, description="Error message if `status` is 'failed'."
    )
    data_version: int | None = Field(
        default=None,
        description=(
            "id of the most recent successful RefreshRun -- the current "
            "graph/route cache version. None if no refresh has ever succeeded."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal warnings from the most recent refresh/graph build, "
            "e.g. graph-density-guardrail notices. Empty when there are none."
        ),
    )
