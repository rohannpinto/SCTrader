"""Unit tests for `backend/models/schemas.py` (Pydantic v2 request/response models)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.models.schemas import (
    CommodityOut,
    RefreshStatusOut,
    RouteHop,
    RouteRequest,
    RouteResponse,
    TerminalOut,
)


# --- CommodityOut / TerminalOut --------------------------------------------


def test_commodity_out_round_trips():
    commodity = CommodityOut(id=1, slug="laranite", name="Laranite")
    dumped = commodity.model_dump()
    assert CommodityOut.model_validate(dumped) == commodity


def test_terminal_out_allows_optional_fields_none():
    terminal = TerminalOut(id=1, name="Some Terminal")
    assert terminal.star_system_name is None
    assert terminal.location_name is None
    assert terminal.planet_name is None
    assert terminal.moon_name is None
    assert terminal.is_orbital_station is False


def test_terminal_out_carries_planetoid_and_orbital_station_fields():
    """Task 12/16: `planet_name`/`moon_name`/`is_orbital_station` feed the
    frontend's System/Planetoid/"include orbital stations" filters."""
    terminal = TerminalOut(
        id=1,
        name="Everus Harbor",
        planet_name="Hurston",
        moon_name=None,
        is_orbital_station=True,
    )
    assert terminal.planet_name == "Hurston"
    assert terminal.moon_name is None
    assert terminal.is_orbital_station is True


def test_terminal_out_from_attributes():
    class _FakeOrmTerminal:
        id = 5
        name = "Port Olisar"
        star_system_name = "Stanton"
        location_name = "Crusader"
        planet_name = "Crusader"
        moon_name = None
        is_orbital_station = False

    terminal = TerminalOut.model_validate(_FakeOrmTerminal())
    assert terminal.id == 5
    assert terminal.name == "Port Olisar"
    assert terminal.planet_name == "Crusader"
    assert terminal.is_orbital_station is False


# --- RouteRequest -----------------------------------------------------------


def test_route_request_accepts_valid_input():
    req = RouteRequest(start_terminal_id=1, ship_id=2, num_hops=5, starting_budget=1000.0)
    assert req.start_terminal_id == 1
    assert req.ship_id == 2
    assert req.num_hops == 5
    assert req.starting_budget == 1000.0


def test_route_request_rejects_non_positive_num_hops():
    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, ship_id=1, num_hops=0, starting_budget=1000.0)

    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, ship_id=1, num_hops=-3, starting_budget=1000.0)


def test_route_request_rejects_negative_starting_budget():
    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=-1.0)


def test_route_request_accepts_zero_starting_budget():
    req = RouteRequest(start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=0.0)
    assert req.starting_budget == 0.0


def test_route_request_risk_level_defaults_to_10():
    """Phase 3, Task 21: a caller that omits `risk_level` entirely must see
    unchanged pre-Phase-3 behavior -- `risk_level=10` maps to `rank=1`,
    today's single best route."""
    req = RouteRequest(start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=1000.0)
    assert req.risk_level == 10


@pytest.mark.parametrize("risk_level", [0, 1, 5, 9, 10])
def test_route_request_accepts_risk_level_in_bounds(risk_level):
    req = RouteRequest(
        start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=1000.0, risk_level=risk_level
    )
    assert req.risk_level == risk_level


def test_route_request_rejects_risk_level_below_zero():
    with pytest.raises(ValidationError):
        RouteRequest(
            start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=1000.0, risk_level=-1
        )


def test_route_request_rejects_risk_level_above_ten():
    with pytest.raises(ValidationError):
        RouteRequest(
            start_terminal_id=1, ship_id=1, num_hops=5, starting_budget=1000.0, risk_level=11
        )


# --- RouteHop / RouteResponse -----------------------------------------------


def _sample_hop(**overrides) -> RouteHop:
    defaults = dict(
        terminal_id=2,
        terminal_name="Terminal B",
        commodity_id=1,
        commodity_name="Laranite",
        distance_from_previous=1234.5,
        quantity_traded=10.0,
        unit_buy_price=100.0,
        unit_sell_price=150.0,
        profit_this_hop=500.0,
    )
    defaults.update(overrides)
    return RouteHop(**defaults)


def _sample_bridge_hop(**overrides) -> RouteHop:
    """A neutral bridge hop -- no commodity traded, matches
    `backend/graph/search.py`'s `RouteHopResult` shape for a hop where
    `commodity_id is None`."""
    defaults = dict(
        terminal_id=3,
        terminal_name="Terminal C",
        commodity_id=None,
        commodity_name=None,
        distance_from_previous=50.0,
        quantity_traded=0.0,
        unit_buy_price=None,
        unit_sell_price=None,
        profit_this_hop=0.0,
    )
    defaults.update(overrides)
    return RouteHop(**defaults)


def test_route_hop_rejects_negative_distance_profit_and_quantity():
    with pytest.raises(ValidationError):
        _sample_hop(distance_from_previous=-1.0)

    with pytest.raises(ValidationError):
        _sample_hop(profit_this_hop=-1.0)

    with pytest.raises(ValidationError):
        _sample_hop(quantity_traded=-1.0)


def test_route_hop_allows_none_commodity_and_prices_for_bridge_hop():
    hop = _sample_bridge_hop()
    assert hop.commodity_id is None
    assert hop.commodity_name is None
    assert hop.unit_buy_price is None
    assert hop.unit_sell_price is None
    assert hop.quantity_traded == 0.0
    assert hop.profit_this_hop == 0.0


def test_route_response_found_case_round_trips():
    hop = _sample_hop()
    response = RouteResponse(
        found=True,
        start_terminal_id=1,
        hops=[hop],
        total_distance=1234.5,
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
    )
    dumped = response.model_dump()
    reloaded = RouteResponse.model_validate(dumped)
    assert reloaded == response
    assert reloaded.found is True
    assert len(reloaded.hops) == 1


def test_route_response_not_found_case_is_unambiguous():
    response = RouteResponse(
        start_terminal_id=1,
        found=False,
        starting_budget=1000.0,
        final_cash=1000.0,
        message="No profitable route found from this start terminal.",
    )
    assert response.found is False
    assert response.hops == []
    assert response.total_distance == 0.0
    assert response.total_profit == 0.0
    assert response.final_cash == response.starting_budget

    dumped = response.model_dump()
    reloaded = RouteResponse.model_validate(dumped)
    assert reloaded == response
    assert reloaded.found is False


def test_route_response_found_and_not_found_are_distinguishable_by_flag_alone():
    found = RouteResponse(
        start_terminal_id=1,
        found=True,
        hops=[_sample_hop()],
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
    )
    not_found = RouteResponse(
        start_terminal_id=1, found=False, starting_budget=1000.0, final_cash=1000.0
    )
    assert found.found != not_found.found
    # `found` is the contract -- not merely inferred from `hops` emptiness.
    assert (len(found.hops) > 0) == found.found
    assert (len(not_found.hops) > 0) == not_found.found


def test_route_response_rejects_found_true_with_empty_hops():
    with pytest.raises(ValidationError):
        RouteResponse(
            start_terminal_id=1, found=True, hops=[], starting_budget=1000.0, final_cash=1000.0
        )


def test_route_response_rejects_found_false_with_nonempty_hops():
    with pytest.raises(ValidationError):
        RouteResponse(
            start_terminal_id=1,
            found=False,
            hops=[_sample_hop()],
            starting_budget=1000.0,
            final_cash=1500.0,
            total_profit=500.0,
        )


def test_route_response_accepts_found_true_with_hops_and_found_false_with_empty_hops():
    found = RouteResponse(
        start_terminal_id=1,
        found=True,
        hops=[_sample_hop()],
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
    )
    assert found.found is True
    assert len(found.hops) == 1

    not_found = RouteResponse(
        start_terminal_id=1, found=False, hops=[], starting_budget=1000.0, final_cash=1000.0
    )
    assert not_found.found is False
    assert not_found.hops == []


# --- RouteResponse: total_profit == final_cash - starting_budget invariant --


def test_route_response_defaults_total_profit_to_zero_when_cash_unchanged():
    response = RouteResponse(
        start_terminal_id=1, found=False, starting_budget=1000.0, final_cash=1000.0
    )
    assert response.total_profit == 0.0


def test_route_response_rejects_total_profit_inconsistent_with_cash_delta():
    with pytest.raises(ValidationError):
        RouteResponse(
            start_terminal_id=1,
            found=True,
            hops=[_sample_hop()],
            starting_budget=1000.0,
            final_cash=1500.0,
            total_profit=999.0,  # should be 500.0 (final_cash - starting_budget)
        )


def test_route_response_accepts_total_profit_matching_cash_delta():
    response = RouteResponse(
        start_terminal_id=1,
        found=True,
        hops=[_sample_hop()],
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
    )
    assert response.total_profit == 500.0


# --- RouteResponse: requested_rank / actual_rank_used (Phase 3, Task 21) ----


def test_route_response_rank_fields_default_to_1_and_0():
    """Mirrors `RouteSearchResult`'s dataclass defaults (`backend/graph/
    search.py`) -- unspecified means "rank=1, no route to attach a used-rank
    to yet"."""
    response = RouteResponse(
        start_terminal_id=1, found=False, starting_budget=1000.0, final_cash=1000.0
    )
    assert response.requested_rank == 1
    assert response.actual_rank_used == 0


def test_route_response_rank_fields_round_trip():
    response = RouteResponse(
        found=True,
        start_terminal_id=1,
        hops=[_sample_hop()],
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
        requested_rank=7,
        actual_rank_used=3,
    )
    dumped = response.model_dump()
    reloaded = RouteResponse.model_validate(dumped)
    assert reloaded == response
    assert reloaded.requested_rank == 7
    assert reloaded.actual_rank_used == 3


def test_route_response_rank_fields_surface_a_clamp():
    """`requested_rank != actual_rank_used` is exactly how a client detects
    that Task 20's clamp-when-insufficient-routes-exist behavior fired."""
    clamped = RouteResponse(
        found=True,
        start_terminal_id=1,
        hops=[_sample_hop()],
        starting_budget=1000.0,
        final_cash=1500.0,
        total_profit=500.0,
        requested_rank=8,
        actual_rank_used=3,
    )
    assert clamped.requested_rank != clamped.actual_rank_used
    assert clamped.actual_rank_used < clamped.requested_rank


@pytest.mark.parametrize("bad_value", [-1, 11])
def test_route_response_rejects_requested_rank_out_of_bounds(bad_value):
    with pytest.raises(ValidationError):
        RouteResponse(
            start_terminal_id=1,
            found=False,
            starting_budget=1000.0,
            final_cash=1000.0,
            requested_rank=bad_value,
        )


def test_route_response_rejects_negative_actual_rank_used():
    with pytest.raises(ValidationError):
        RouteResponse(
            start_terminal_id=1,
            found=False,
            starting_budget=1000.0,
            final_cash=1000.0,
            actual_rank_used=-1,
        )


# --- RefreshStatusOut --------------------------------------------------------


def test_refresh_status_out_defaults_warnings_to_empty_list():
    status = RefreshStatusOut(status="never_run")
    assert status.warnings == []
    assert status.data_version is None


def test_refresh_status_out_round_trips_with_warnings():
    status = RefreshStatusOut(
        status="success",
        started_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 14, 12, 5, tzinfo=timezone.utc),
        commodities_count=100,
        terminals_count=50,
        prices_count=2000,
        distances_count=2500,
        data_version=3,
        warnings=["graph edge count 60000 exceeds guardrail 50000"],
    )
    dumped = status.model_dump()
    reloaded = RefreshStatusOut.model_validate(dumped)
    assert reloaded == status
    assert reloaded.warnings == ["graph edge count 60000 exceeds guardrail 50000"]


def test_refresh_status_out_rejects_negative_counts():
    with pytest.raises(ValidationError):
        RefreshStatusOut(status="success", commodities_count=-1)
