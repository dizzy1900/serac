from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from serac.domain import avoided_loss
from serac.domain.avoided_loss import (
    AvoidedLossRequest,
    AvoidedLossResponse,
    AvoidedLossStatus,
    ExposureItem,
    InterventionKind,
    MoneyRange,
    ScenarioLoss,
    WarningScenario,
)
from serac.domain.common import Range
from serac.domain.events import AssetType
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    ForecastModel,
    ModelProvenance,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
RangeFactory = Callable[..., Range]


def _money(**overrides: Any) -> MoneyRange:
    data: dict[str, Any] = {
        "low": 1.0,
        "high": 2.0,
        "currency": "USD",
        "price_year": 2026,
        "basis": "fictional placeholder",
    }
    data.update(overrides)
    return MoneyRange(**data)


def test_money_range() -> None:
    assert _money(best=1.5).best == 1.5
    with pytest.raises(ValidationError, match=r"low=3\.0 exceeds high=2\.0"):
        _money(low=3.0)
    with pytest.raises(ValidationError, match=r"best=9.0 outside \[1.0, 2.0\]"):
        _money(best=9.0)
    with pytest.raises(ValidationError, match="currency"):
        _money(currency="usd")
    with pytest.raises(ValidationError, match="low"):
        _money(low=-1.0)


def _baseline() -> WarningScenario:
    return WarningScenario(
        scenario_id="baseline", intervention=InterventionKind.none, description="no warning"
    )


def _warning(make_range: RangeFactory, scenario_id: str = "warning") -> WarningScenario:
    return WarningScenario(
        scenario_id=scenario_id,
        intervention=InterventionKind.warning,
        lead_time_min=make_range(unit="min"),
        description="fictional warning",
    )


def test_warning_scenario_lead_time_rules(make_range: RangeFactory) -> None:
    with pytest.raises(
        ValidationError, match="baseline: intervention=none cannot carry lead_time_min"
    ):
        WarningScenario(
            scenario_id="baseline",
            intervention=InterventionKind.none,
            lead_time_min=make_range(unit="min"),
            description="x",
        )
    with pytest.raises(ValidationError, match="warning: intervention=warning needs lead_time_min"):
        WarningScenario(
            scenario_id="warning", intervention=InterventionKind.warning, description="x"
        )
    assert _warning(make_range).lead_time_min is not None


def _forecast(make_range: RangeFactory) -> CascadeForecast:
    return CascadeForecast(
        forecast_id="test-forecast",
        aoi_id="test-aoi",
        issued_utc=NOW,
        origin_time_utc=NOW,
        source_volume_m3=make_range(unit="m3", source_refs=["test-run"]),
        runout_km=make_range(unit="km", source_refs=["test-run"]),
        model=ForecastModel(name="test", version="0", provenance=ModelProvenance.stub),
        confidence_tier=ConfidenceTier.unqualified,
    )


def _request(make_range: RangeFactory, **overrides: Any) -> AvoidedLossRequest:
    data: dict[str, Any] = {
        "request_id": "test-request",
        "requested_utc": NOW,
        "forecast": _forecast(make_range),
        "exposure": [ExposureItem(asset_id="test-asset", asset_type=AssetType.bridge)],
        "scenarios": [_baseline(), _warning(make_range)],
    }
    data.update(overrides)
    return AvoidedLossRequest(**data)


def test_request_valid_and_pinned_version(make_range: RangeFactory) -> None:
    req = _request(make_range)
    assert req.contract_version == "0.1.0"
    with pytest.raises(ValidationError, match="contract_version"):
        _request(make_range, contract_version="0.0.0")
    again = AvoidedLossRequest.model_validate(req.model_dump(mode="json"))
    assert again == req


def test_request_needs_baseline_scenario(make_range: RangeFactory) -> None:
    with pytest.raises(ValidationError, match="must include one baseline with intervention='none'"):
        _request(make_range, scenarios=[_warning(make_range)])


def test_request_unique_ids(make_range: RangeFactory) -> None:
    with pytest.raises(ValidationError, match=r"scenarios: scenario_id repeated \['baseline'\]"):
        _request(make_range, scenarios=[_baseline(), _baseline()])
    item = ExposureItem(asset_id="test-asset", asset_type=AssetType.bridge)
    with pytest.raises(ValidationError, match=r"exposure: asset_id repeated \['test-asset'\]"):
        _request(make_range, exposure=[item, item])
    with pytest.raises(ValidationError, match="exposure"):
        _request(make_range, exposure=[])


def _response(**overrides: Any) -> AvoidedLossResponse:
    data: dict[str, Any] = {
        "request_id": "test-request",
        "status": AvoidedLossStatus.not_implemented,
        "computed_utc": NOW,
        "assumptions": ["not implemented"],
    }
    data.update(overrides)
    return AvoidedLossResponse(**data)


def test_response_not_implemented_carries_no_losses() -> None:
    assert _response().losses == []
    loss = ScenarioLoss(scenario_id="baseline", expected_loss=_money())
    with pytest.raises(ValidationError, match="losses: must be empty when status=not_implemented"):
        _response(losses=[loss])
    with pytest.raises(ValidationError, match="assumptions"):
        _response(assumptions=[])
    with pytest.raises(ValidationError, match="status"):
        _response(status="estimated")


def test_response_computed_needs_losses_and_model() -> None:
    loss = ScenarioLoss(scenario_id="baseline", expected_loss=_money())
    model = ForecastModel(name="test", version="0", provenance=ModelProvenance.surrogate)
    with pytest.raises(ValidationError, match="losses: must be non-empty when status=computed"):
        _response(status=AvoidedLossStatus.computed, model=model)
    with pytest.raises(ValidationError, match="model: required when status=computed"):
        _response(status=AvoidedLossStatus.computed, losses=[loss])
    ok = _response(status=AvoidedLossStatus.computed, losses=[loss], model=model)
    assert ok.status is AvoidedLossStatus.computed


def test_contracts_table() -> None:
    assert {
        "avoided-loss": AvoidedLossRequest,
        "avoided-loss-response": AvoidedLossResponse,
        "asset-scenario-loss": avoided_loss.AssetScenarioLoss,
    } == avoided_loss.CONTRACTS
