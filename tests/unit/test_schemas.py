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


def test_terminal_out_from_attributes():
    class _FakeOrmTerminal:
        id = 5
        name = "Port Olisar"
        star_system_name = "Stanton"
        location_name = "Crusader"

    terminal = TerminalOut.model_validate(_FakeOrmTerminal())
    assert terminal.id == 5
    assert terminal.name == "Port Olisar"


# --- RouteRequest -----------------------------------------------------------


def test_route_request_accepts_valid_input():
    req = RouteRequest(start_terminal_id=1, max_distance=1000.0)
    assert req.distance_threshold is None


def test_route_request_rejects_non_positive_max_distance():
    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, max_distance=0)

    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, max_distance=-5.0)


def test_route_request_rejects_non_positive_distance_threshold_when_provided():
    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, max_distance=1000.0, distance_threshold=0)

    with pytest.raises(ValidationError):
        RouteRequest(start_terminal_id=1, max_distance=1000.0, distance_threshold=-1.0)


def test_route_request_accepts_positive_distance_threshold_override():
    req = RouteRequest(start_terminal_id=1, max_distance=1000.0, distance_threshold=5000.0)
    assert req.distance_threshold == 5000.0


# --- RouteHop / RouteResponse -----------------------------------------------


def _sample_hop(**overrides) -> RouteHop:
    defaults = dict(
        terminal_id=2,
        terminal_name="Terminal B",
        commodity_id=1,
        commodity_name="Laranite",
        distance_from_previous=1234.5,
        profit_this_hop=42.0,
    )
    defaults.update(overrides)
    return RouteHop(**defaults)


def test_route_hop_rejects_negative_distance_and_profit():
    with pytest.raises(ValidationError):
        _sample_hop(distance_from_previous=-1.0)

    with pytest.raises(ValidationError):
        _sample_hop(profit_this_hop=-1.0)


def test_route_response_found_case_round_trips():
    hop = _sample_hop()
    response = RouteResponse(
        found=True,
        start_terminal_id=1,
        hops=[hop],
        total_distance=1234.5,
        total_profit=42.0,
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
        message="No profitable route found from this start terminal.",
    )
    assert response.found is False
    assert response.hops == []
    assert response.total_distance == 0.0
    assert response.total_profit == 0.0

    dumped = response.model_dump()
    reloaded = RouteResponse.model_validate(dumped)
    assert reloaded == response
    assert reloaded.found is False


def test_route_response_found_and_not_found_are_distinguishable_by_flag_alone():
    found = RouteResponse(start_terminal_id=1, found=True, hops=[_sample_hop()])
    not_found = RouteResponse(start_terminal_id=1, found=False)
    assert found.found != not_found.found
    # `found` is the contract -- not merely inferred from `hops` emptiness.
    assert (len(found.hops) > 0) == found.found
    assert (len(not_found.hops) > 0) == not_found.found


def test_route_response_rejects_found_true_with_empty_hops():
    with pytest.raises(ValidationError):
        RouteResponse(start_terminal_id=1, found=True, hops=[])


def test_route_response_rejects_found_false_with_nonempty_hops():
    with pytest.raises(ValidationError):
        RouteResponse(start_terminal_id=1, found=False, hops=[_sample_hop()])


def test_route_response_accepts_found_true_with_hops_and_found_false_with_empty_hops():
    found = RouteResponse(start_terminal_id=1, found=True, hops=[_sample_hop()])
    assert found.found is True
    assert len(found.hops) == 1

    not_found = RouteResponse(start_terminal_id=1, found=False, hops=[])
    assert not_found.found is False
    assert not_found.hops == []


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
