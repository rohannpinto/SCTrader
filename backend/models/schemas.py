"""Pydantic v2 request/response models -- the API contract for the FastAPI
routers built in later tasks.

These models are intentionally decoupled from the SQLAlchemy models in
`backend/models/db.py`: `*Out` models expose only the fields a client needs
(and can be built from ORM rows via `model_validate(orm_obj)` thanks to
`from_attributes=True`), and request models add explicit validation bounds
per CLAUDE.md's security ground rules ("all user-facing input must be
validated server-side with explicit bounds").

Note: server-side caps (e.g. `distance_threshold_max`, `max_distance_cap`
from `backend.config.Settings`) and existence checks (e.g. "does this
`start_terminal_id` actually exist") are enforced by the routers that use
these models, not here -- these models only enforce shape/type/sign
constraints that hold true regardless of runtime config.
"""

from __future__ import annotations

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


class RouteRequest(BaseModel):
    """A request to search for a profitable trading route from a starting terminal."""

    start_terminal_id: int = Field(
        ..., description="Internal Terminal.id to start the route search from."
    )
    max_distance: float = Field(
        ...,
        gt=0,
        description=(
            "Total in-game distance budget for the route search. Must be "
            "positive; also capped server-side at `settings.max_distance_cap`."
        ),
    )
    distance_threshold: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional per-request override of the server's default max "
            "edge distance (`settings.distance_threshold_default`). Must "
            "be positive if provided; also capped server-side at "
            "`settings.distance_threshold_max`."
        ),
    )


class RouteHop(BaseModel):
    """A single leg of a found route: travel to a terminal and trade a commodity there."""

    terminal_id: int = Field(..., description="Internal Terminal.id of this hop's destination.")
    terminal_name: str = Field(..., description="Human-readable name of this hop's destination.")
    commodity_id: int = Field(
        ..., description="Internal Commodity.id of the commodity traded on this hop."
    )
    commodity_name: str = Field(
        ..., description="Human-readable name of the commodity traded on this hop."
    )
    distance_from_previous: float = Field(
        ..., ge=0, description="In-game distance travelled from the previous hop (or start)."
    )
    profit_this_hop: float = Field(
        ..., ge=0, description="Profit realized by buying/selling on this hop's edge."
    )


class RouteResponse(BaseModel):
    """Result of a route search.

    Explicitly and unambiguously represents both possible outcomes via the
    `found` flag:

    - `found=True`: `hops` is a non-empty ordered list of `RouteHop`,
      `total_distance` and `total_profit` summarize the whole route.
    - `found=False`: no profitable route existed from `start_terminal_id`
      under the given constraints (including the "isolated start node, zero
      viable outgoing edges" case from CLAUDE.md). This is a normal,
      successful response -- not an error -- so `hops` is an empty list,
      `total_distance`/`total_profit` are `0.0`, and `message` carries a
      human-readable explanation for direct display.

    Callers should branch on `found`, never infer it from `hops` being
    empty (kept consistent by construction, but `found` is the contract).
    """

    found: bool = Field(..., description="Whether a profitable route was found.")
    start_terminal_id: int = Field(..., description="Echoes the requested starting terminal.")
    hops: list[RouteHop] = Field(
        default_factory=list, description="Ordered route legs; empty when `found` is False."
    )
    total_distance: float = Field(
        default=0.0, ge=0, description="Sum of `distance_from_previous` across all hops."
    )
    total_profit: float = Field(
        default=0.0, ge=0, description="Sum of `profit_this_hop` across all hops."
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
