"""The avoided-loss engine: damage functions, missing-input handling, aggregation.

The most important assertion in this file is `test_an_asset_with_no_usable_input_is_never_zero`.
Everything else is arithmetic; that one is the design.
"""

from __future__ import annotations

import pytest

from serac.alerting.example import check_forecast, check_request
from serac.cascade.compute import (
    INSUFFICIENT_INPUT_PREFIX,
    BlockReason,
    compute_avoided_loss,
    replacement_value_for,
)
from serac.cascade.damage import (
    ASSUMPTION_MARKER,
    BRIDGE,
    DAMAGE_FUNCTIONS,
    HYDROPOWER_HEADWORKS,
    HYDROPOWER_POWERHOUSE,
    SETTLEMENT,
    WARNING_BENEFITS,
    DamageFunction,
    ParameterProvenance,
    ReplacementValueRule,
    damage_function_for,
)
from serac.domain.avoided_loss import (
    AvoidedLossStatus,
    ExposureItem,
    InterventionKind,
    MoneyRange,
)
from serac.domain.common import Range
from serac.domain.events import AssetType

# -- damage functions ----------------------------------------------------------------------------


@pytest.mark.parametrize("function", DAMAGE_FUNCTIONS, ids=lambda f: f.id)
def test_every_damage_function_declares_its_provenance(function: DamageFunction) -> None:
    """The brief: a cited source, or an explicit assumption marker. serac has no sources."""
    assert function.provenance is ParameterProvenance.assumption
    assert function.source_url is None
    assert function.assumption.startswith(ASSUMPTION_MARKER)
    assert function.rationale


@pytest.mark.parametrize("function", DAMAGE_FUNCTIONS, ids=lambda f: f.id)
def test_damage_is_zero_at_zero_depth_monotone_and_bounded(function: DamageFunction) -> None:
    assert function.central(0.0) == 0.0
    assert function.central(-1.0) == 0.0
    previous = 0.0
    for depth in (0.1, 0.5, 1.0, 2.0, 5.0, 20.0, 200.0):
        value = function.central(depth)
        assert 0.0 <= value <= 1.0
        assert value >= previous
        previous = value
    assert function.central(1000.0) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("function", DAMAGE_FUNCTIONS, ids=lambda f: f.id)
def test_the_interval_brackets_the_central_value(function: DamageFunction) -> None:
    low, high = function.interval(2.0, 4.0)
    assert low <= function.central(2.0) <= high
    assert low <= function.central(4.0) <= high


def test_headworks_are_more_vulnerable_than_a_powerhouse_at_the_same_depth() -> None:
    """The physical claim the two hydropower functions encode."""
    assert HYDROPOWER_HEADWORKS.central(1.0) > HYDROPOWER_POWERHOUSE.central(1.0)


def test_a_bridge_fails_more_abruptly_than_a_settlement() -> None:
    assert BRIDGE.shape > SETTLEMENT.shape


def test_a_hydropower_asset_gets_both_component_functions() -> None:
    functions = damage_function_for(AssetType.hydropower_plant)
    assert {f.id for f in functions} == {HYDROPOWER_HEADWORKS.id, HYDROPOWER_POWERHOUSE.id}


def test_a_non_finite_depth_is_refused_rather_than_producing_a_nan() -> None:
    with pytest.raises(ValueError, match="finite"):
        SETTLEMENT.central(float("nan"))


def test_damage_function_rejects_a_crossed_d0_interval() -> None:
    with pytest.raises(ValueError, match="d0_high_damage_m <= d0_central_m"):
        DamageFunction(
            id="bad",
            applies_to=(AssetType.other,),
            d0_high_damage_m=9.0,
            d0_central_m=5.0,
            d0_low_damage_m=1.0,
            shape=1.0,
            rationale="crossed",
        )


# -- warning benefit -----------------------------------------------------------------------------


def test_no_lead_time_avoids_nothing_and_the_ramp_saturates() -> None:
    benefit = WARNING_BENEFITS[AssetType.hydropower_plant]
    assert benefit.avoidable_share(0.0) == (0.0, 0.0)
    assert benefit.avoidable_share(benefit.lead_time_threshold_min) == (0.0, 0.0)
    low, high = benefit.avoidable_share(benefit.lead_time_full_min * 10)
    assert (low, high) == (benefit.max_avoidable_share_low, benefit.max_avoidable_share_high)


def test_a_warning_cannot_save_much_of_a_bridge() -> None:
    low, high = WARNING_BENEFITS[AssetType.bridge].avoidable_share(1000.0)
    assert low == 0.0
    assert high <= 0.05


# -- replacement values --------------------------------------------------------------------------


def test_a_caller_supplied_value_wins() -> None:
    supplied = MoneyRange(
        low=1.0, high=2.0, currency="USD", price_year=2026, basis="the caller's schedule"
    )
    item = ExposureItem(
        asset_id="a", asset_type=AssetType.bridge, transect_id="t", replacement_value=supplied
    )
    assert replacement_value_for(item, None, ReplacementValueRule()) is supplied


def test_a_hydropower_value_is_derived_from_capacity_and_says_so() -> None:
    item = ExposureItem(asset_id="a", asset_type=AssetType.hydropower_plant, transect_id="t")
    capacity = Range(low=100.0, high=100.0, best=100.0, unit="MW", source_refs=["doed"])
    value = replacement_value_for(item, capacity, ReplacementValueRule())
    assert value is not None
    assert value.low == pytest.approx(150e6)
    assert value.high == pytest.approx(400e6)
    assert value.best is None  # no qualifying source supports a central value
    assert "assumption" in value.basis


@pytest.mark.parametrize(
    "asset_type",
    [AssetType.bridge, AssetType.settlement, AssetType.border_post, AssetType.road],
)
def test_no_value_is_invented_for_an_asset_serac_knows_nothing_about(
    asset_type: AssetType,
) -> None:
    item = ExposureItem(asset_id="a", asset_type=asset_type, transect_id="t")
    assert replacement_value_for(item, None, ReplacementValueRule()) is None


# -- the engine ----------------------------------------------------------------------------------


def test_a_complete_request_computes_and_carries_every_assumption() -> None:
    result = compute_avoided_loss(check_request())
    assert result.response.status is AvoidedLossStatus.computed
    assert result.response.model is not None
    assert len(result.response.losses) == 2
    assert any(a.startswith(ASSUMPTION_MARKER) for a in result.response.assumptions)
    # Lives are never counted here: there is no sourced population anywhere.
    assert result.lives_in_warned_zone is None
    assert all(loss.expected_fatalities is None for loss in result.response.losses)


def test_a_warning_avoids_something_but_not_everything() -> None:
    result = compute_avoided_loss(check_request())
    baseline = next(x for x in result.response.losses if x.scenario_id == "no-warning")
    warned = next(x for x in result.response.losses if x.scenario_id == "warning")
    assert warned.expected_loss.high <= baseline.expected_loss.high
    assert warned.avoided_vs_baseline is not None
    assert warned.avoided_vs_baseline.high > 0
    assert baseline.avoided_vs_baseline is None


def test_an_asset_with_no_usable_input_is_never_zero() -> None:
    """The design assertion: an undetermined asset must not read as 'no loss expected'."""
    result = compute_avoided_loss(check_request())
    unreached = [x for x in result.by_asset if x.asset_id == "fictional-unreached"]
    assert unreached, "the fixture must contain an asset at an unreached transect"
    for row in unreached:
        assert row.determined is False
        assert row.blocked_by is BlockReason.no_arrival
        assert row.expected_loss is None
        assert row.avoided_vs_baseline is None
        assert "NOT a statement that the asset is safe" in (row.blocked_detail or "")
    assert "fictional-unreached" in result.undetermined
    assert "fictional-unreached" not in result.determined_asset_ids


def test_the_totals_say_how_many_assets_they_cover() -> None:
    result = compute_avoided_loss(check_request())
    for loss in result.response.losses:
        assert "undetermined" in loss.expected_loss.basis
        assert "NOT counted as zero" in loss.expected_loss.basis


def test_a_forecast_with_no_depths_produces_an_insufficient_input_response() -> None:
    forecast = check_forecast()
    depthless = forecast.model_copy(
        update={
            "transect_arrivals": [
                a.model_copy(update={"peak_stage_m": None}) for a in forecast.transect_arrivals
            ]
        }
    )
    result = compute_avoided_loss(check_request(depthless))
    assert result.response.status is AvoidedLossStatus.not_implemented
    assert result.response.losses == []
    assert (result.response.notes or "").startswith(INSUFFICIENT_INPUT_PREFIX)
    assert set(result.undetermined.values()) == {
        BlockReason.no_flow_depth,
        BlockReason.no_arrival,
    }
    # The hazard input is still named, so a reader knows what produced nothing.
    assert result.response.model is not None


def test_an_asset_with_no_transect_is_blocked_on_that_first() -> None:
    request = check_request()
    stripped = request.model_copy(
        update={
            "exposure": [
                request.exposure[0].model_copy(update={"transect_id": None}),
                *request.exposure[1:],
            ]
        }
    )
    result = compute_avoided_loss(stripped)
    assert result.undetermined[request.exposure[0].asset_id] is BlockReason.no_transect


def test_the_forecasts_own_lead_time_beats_the_scenarios() -> None:
    """A per-transect lead time from the forecast is more specific than one span."""
    request = check_request()
    result = compute_avoided_loss(request)
    row = next(
        x
        for x in result.by_asset
        if x.asset_id == "fictional-village" and x.scenario_id == "warning"
    )
    arrival = next(
        a for a in request.forecast.transect_arrivals if a.transect_id == "fictional-transect-b"
    )
    assert arrival.lead_time_min is not None
    assert row.lead_time_min == arrival.lead_time_min


def test_replacement_values_can_come_from_the_capacities_map() -> None:
    request = check_request(with_values=False)
    result = compute_avoided_loss(
        request,
        capacities={
            "fictional-plant": Range(low=10.0, high=10.0, unit="MW", source_refs=["fictional"])
        },
    )
    plant = next(x for x in result.by_asset if x.asset_id == "fictional-plant" and x.determined)
    assert plant.replacement_value is not None
    assert result.undetermined["fictional-bridge"] is BlockReason.no_replacement_value


def test_the_baseline_scenario_is_required_by_the_contract() -> None:
    request = check_request()
    with pytest.raises(ValueError, match="must include one baseline"):
        request.model_copy(
            update={
                "scenarios": [
                    s for s in request.scenarios if s.intervention is not InterventionKind.none
                ]
            }
        ).model_validate(
            {
                **request.model_dump(mode="json"),
                "scenarios": [
                    s.model_dump(mode="json")
                    for s in request.scenarios
                    if s.intervention is not InterventionKind.none
                ],
            }
        )
