"""A deliberately fictional `CascadeForecast` for exercising the alerting and loss paths.

`validate-e2e` has to prove that the CAP generator produces XSD-valid XML and that a
`computed` avoided-loss response validates against the committed contract. Neither can be
proved on the real replays, because both chains stop before a forecast exists. So the gate
uses this: a forecast about **no real place**, with `event="fictional-check"`, an AOI id that
matches no AOI in `data/aoi/`, and `FICTIONAL_FORECAST_NOTICE` on every `Range` and in
`assumptions[]`.

This is the same pattern `serac.validation.underwriting.example_request` already uses for the
Prompt 1 schema round-trip. It is code, not data: nothing here is written under `data/`, and
`ModelProvenance.stub` forces `confidence_tier=unqualified`, which forces any CAP message
built from it to `status: Test`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from serac.domain.avoided_loss import (
    AvoidedLossRequest,
    ExposureItem,
    InterventionKind,
    MoneyRange,
    WarningScenario,
)
from serac.domain.common import Range
from serac.domain.events import AssetType
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    DammingEstimate,
    ForecastModel,
    ModelProvenance,
    SecondarySurge,
    TransectArrival,
)
from serac.domain.geometry import Polygon

FICTIONAL_FORECAST_NOTICE = (
    "FICTIONAL check fixture built in code so the CAP renderer and the avoided-loss engine can "
    "be exercised end to end. It describes no real place, event, asset or model run, and no "
    "figure in it is an estimate of anything."
)

CHECK_SOURCE_REF = "fictional-check-forecast"
CHECK_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def _range(low: float, best: float | None, high: float, unit: str) -> Range:
    return Range(
        low=low,
        high=high,
        best=best,
        unit=unit,
        source_refs=[CHECK_SOURCE_REF],
        notes=FICTIONAL_FORECAST_NOTICE,
    )


def check_forecast(
    *,
    provenance: ModelProvenance = ModelProvenance.stub,
    confidence_tier: ConfidenceTier = ConfidenceTier.unqualified,
    with_footprint: bool = True,
) -> CascadeForecast:
    """A complete forecast: footprint, two transect arrivals with stages, damming and a surge."""
    footprint = (
        Polygon(
            coordinates=[
                [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01), (0.0, 0.0)],
            ]
        )
        if with_footprint
        else None
    )
    return CascadeForecast(
        forecast_id="fictional-check-forecast",
        aoi_id="fictional-check-aoi",
        event_id=None,
        detection_id=None,
        issued_utc=CHECK_TIME,
        origin_time_utc=CHECK_TIME,
        source_location=None,
        source_volume_m3=_range(1.0e6, 2.0e6, 3.0e6, "m3"),
        runout_km=_range(1.0, 2.0, 3.0, "km"),
        footprint=footprint,
        transect_arrivals=[
            TransectArrival(
                transect_id="fictional-transect-a",
                arrival_time_min=_range(10.0, 20.0, 30.0, "min"),
                peak_stage_m=_range(1.0, 3.0, 6.0, "m"),
                peak_discharge_m3s=_range(100.0, 300.0, 900.0, "m3s"),
                lead_time_min=_range(5.0, 15.0, 25.0, "min"),
            ),
            TransectArrival(
                transect_id="fictional-transect-b",
                arrival_time_min=_range(40.0, 60.0, 90.0, "min"),
                peak_stage_m=_range(0.5, 1.0, 2.0, "m"),
                peak_discharge_m3s=None,
                lead_time_min=_range(35.0, 55.0, 85.0, "min"),
            ),
        ],
        damming=DammingEstimate(
            probability=Range(
                low=0.05,
                high=0.4,
                best=0.2,
                unit="probability",
                source_refs=[CHECK_SOURCE_REF],
                notes=FICTIONAL_FORECAST_NOTICE,
            ),
            dam_location=None,
            dam_height_m=_range(2.0, 5.0, 9.0, "m"),
            lake_volume_m3=_range(1.0e4, 5.0e4, 2.0e5, "m3"),
            breach_time_after_formation_min=_range(30.0, 90.0, 240.0, "min"),
            secondary_surges=[
                SecondarySurge(
                    origin_chainage_km=1.0,
                    transect_id="fictional-transect-b",
                    arrival_after_breach_min=_range(5.0, 10.0, 20.0, "min"),
                    peak_discharge_m3s=_range(50.0, 100.0, 200.0, "m3s"),
                    label="fictional check fixture",
                )
            ],
        ),
        model=ForecastModel(
            name="serac-fictional-check",
            version="0",
            provenance=provenance,
            run_id=None,
        ),
        confidence_tier=confidence_tier,
        assumptions=[FICTIONAL_FORECAST_NOTICE],
    )


def check_request(
    forecast: CascadeForecast | None = None, *, with_values: bool = True
) -> AvoidedLossRequest:
    """A request whose exposure carries caller-supplied replacement values, so it can compute."""
    hazard = forecast or check_forecast()
    value = (
        MoneyRange(
            low=10.0e6,
            high=30.0e6,
            best=None,
            currency="USD",
            price_year=2000,
            basis=FICTIONAL_FORECAST_NOTICE,
        )
        if with_values
        else None
    )
    return AvoidedLossRequest(
        request_id="fictional-check-request",
        requested_utc=CHECK_TIME,
        requester="serac validate-e2e",
        forecast=hazard,
        exposure=[
            ExposureItem(
                asset_id="fictional-plant",
                asset_type=AssetType.hydropower_plant,
                transect_id="fictional-transect-a",
                replacement_value=value,
                population=None,
            ),
            ExposureItem(
                asset_id="fictional-bridge",
                asset_type=AssetType.bridge,
                transect_id="fictional-transect-a",
                replacement_value=value,
                population=None,
            ),
            ExposureItem(
                asset_id="fictional-village",
                asset_type=AssetType.settlement,
                transect_id="fictional-transect-b",
                replacement_value=value,
                population=None,
            ),
            ExposureItem(
                asset_id="fictional-unreached",
                asset_type=AssetType.settlement,
                transect_id="fictional-transect-z",
                replacement_value=value,
                population=None,
            ),
        ],
        scenarios=[
            WarningScenario(
                scenario_id="no-warning",
                intervention=InterventionKind.none,
                description="no warning issued (baseline)",
                assumptions=[FICTIONAL_FORECAST_NOTICE],
            ),
            WarningScenario(
                scenario_id="warning",
                intervention=InterventionKind.warning,
                lead_time_min=_range(5.0, 15.0, 25.0, "min"),
                description="a warning at the forecast's own lead time",
                assumptions=[FICTIONAL_FORECAST_NOTICE],
            ),
        ],
    )
