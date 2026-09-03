"""L3 cascade forecast contract: interface only.

Nothing in serac populates a `CascadeForecast` until Prompt 2 delivers the runout surrogate.
Stubs may build one with `ForecastModel.provenance == "stub"`, which forces
`confidence_tier == "unqualified"`. Every numeric output is a `Range` whose `source_refs`
name the model run (or fixture) that produced it, never a document.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from serac.domain.common import DOMAIN_CONFIG, SEMVER_PATTERN, Range, Slug
from serac.domain.geometry import MultiPolygon, Point, Polygon

FORECAST_CONTRACT_VERSION = "0.1.0"


class ModelProvenance(StrEnum):
    """What produced the numbers."""

    stub = "stub"
    surrogate = "surrogate"
    simulator = "simulator"


class ConfidenceTier(StrEnum):
    """Qualitative confidence; `unqualified` is the only tier a stub may claim."""

    unqualified = "unqualified"
    low = "low"
    medium = "medium"
    high = "high"


class ForecastModel(BaseModel):
    """Identity of the model that produced a forecast."""

    model_config = DOMAIN_CONFIG

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    provenance: ModelProvenance
    run_id: Slug | None = None


class TransectArrival(BaseModel):
    """Forecast arrival at one transect. Times are minutes after `origin_time_utc`."""

    model_config = DOMAIN_CONFIG

    transect_id: Slug
    arrival_time_min: Range
    peak_stage_m: Range | None = None
    peak_discharge_m3s: Range | None = None
    lead_time_min: Range | None = Field(
        default=None, description="Minutes between the first alert and arrival"
    )


class DammingEstimate(BaseModel):
    """Whether, where and how large a landslide dam / barrier lake may form."""

    model_config = DOMAIN_CONFIG

    probability: Range
    dam_location: Point | None = None
    dam_height_m: Range | None = None
    lake_volume_m3: Range | None = None
    breach_time_after_formation_min: Range | None = None

    @model_validator(mode="after")
    def _probability_bounds(self) -> Self:
        p = self.probability
        if p.unit != "probability" or p.low < 0.0 or p.high > 1.0:
            raise ValueError("probability: unit must be 'probability' with bounds within [0, 1]")
        return self


class CascadeForecast(BaseModel):
    """The L3 output for one detection: footprint, arrivals per transect, damming."""

    model_config = DOMAIN_CONFIG

    contract_version: str = Field(default=FORECAST_CONTRACT_VERSION, pattern=SEMVER_PATTERN)
    forecast_id: Slug
    aoi_id: Slug
    event_id: Slug | None = Field(default=None, description="Set when replaying a known event")
    detection_id: Slug | None = None
    issued_utc: AwareDatetime
    origin_time_utc: AwareDatetime
    source_location: Point | None = None
    source_volume_m3: Range
    runout_km: Range
    footprint: Polygon | MultiPolygon | None = None
    transect_arrivals: list[TransectArrival] = Field(default_factory=list)
    damming: DammingEstimate | None = None
    model: ForecastModel
    confidence_tier: ConfidenceTier
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        if (
            self.model.provenance == ModelProvenance.stub
            and self.confidence_tier != ConfidenceTier.unqualified
        ):
            problems.append("confidence_tier: a stub model may only claim 'unqualified'")
        if self.issued_utc < self.origin_time_utc:
            problems.append("issued_utc precedes origin_time_utc")
        ids = [t.transect_id for t in self.transect_arrivals]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"transect_arrivals: transect_id repeated {duplicates}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"cascade-forecast": CascadeForecast}
