from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from serac.domain import forecast
from serac.domain.common import Range
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    DammingEstimate,
    ForecastModel,
    ModelProvenance,
    TransectArrival,
)
from serac.domain.geometry import Point

NOW = datetime(2026, 1, 1, tzinfo=UTC)
RangeFactory = Callable[..., Range]


def _model(provenance: ModelProvenance = ModelProvenance.stub) -> ForecastModel:
    return ForecastModel(name="test-model", version="0", provenance=provenance)


def _forecast(make_range: RangeFactory, **overrides: Any) -> CascadeForecast:
    data: dict[str, Any] = {
        "forecast_id": "test-forecast",
        "aoi_id": "test-aoi",
        "issued_utc": NOW,
        "origin_time_utc": NOW,
        "source_volume_m3": make_range(unit="m3", source_refs=["test-run"]),
        "runout_km": make_range(unit="km", source_refs=["test-run"]),
        "model": _model(),
        "confidence_tier": ConfidenceTier.unqualified,
    }
    data.update(overrides)
    return CascadeForecast(**data)


def test_stub_forecast_valid(make_range: RangeFactory) -> None:
    fc = _forecast(make_range)
    assert fc.contract_version == forecast.FORECAST_CONTRACT_VERSION
    assert fc.transect_arrivals == [] and fc.damming is None


def test_stub_cannot_claim_confidence(make_range: RangeFactory) -> None:
    with pytest.raises(ValidationError, match="a stub model may only claim 'unqualified'"):
        _forecast(make_range, confidence_tier=ConfidenceTier.high)
    fc = _forecast(
        make_range,
        model=_model(ModelProvenance.surrogate),
        confidence_tier=ConfidenceTier.low,
    )
    assert fc.confidence_tier is ConfidenceTier.low


def test_issued_after_origin(make_range: RangeFactory) -> None:
    with pytest.raises(ValidationError, match="issued_utc precedes origin_time_utc"):
        _forecast(make_range, issued_utc=NOW - timedelta(minutes=1))


def test_transect_arrivals_unique(make_range: RangeFactory) -> None:
    arrival = TransectArrival(transect_id="test-t", arrival_time_min=make_range(unit="min"))
    with pytest.raises(
        ValidationError, match=r"transect_arrivals: transect_id repeated \['test-t'\]"
    ):
        _forecast(make_range, transect_arrivals=[arrival, arrival])
    fc = _forecast(make_range, transect_arrivals=[arrival])
    assert fc.transect_arrivals[0].peak_stage_m is None


def test_damming_probability_bounds(make_range: RangeFactory) -> None:
    with pytest.raises(ValidationError, match="unit must be 'probability'"):
        DammingEstimate(probability=make_range(low=0.1, high=0.5, unit="fraction"))
    with pytest.raises(ValidationError, match=r"within \[0, 1\]"):
        DammingEstimate(probability=make_range(low=0.1, high=1.5, unit="probability"))
    with pytest.raises(ValidationError, match=r"within \[0, 1\]"):
        DammingEstimate(probability=make_range(low=-0.1, high=0.5, unit="probability"))
    dam = DammingEstimate(
        probability=make_range(low=0.1, high=0.5, unit="probability"),
        dam_location=Point(coordinates=(85.5, 28.2)),
    )
    fc = _forecast(make_range, damming=dam)
    assert fc.damming is not None and fc.damming.dam_height_m is None


def test_contracts_table() -> None:
    assert {"cascade-forecast": CascadeForecast} == forecast.CONTRACTS
