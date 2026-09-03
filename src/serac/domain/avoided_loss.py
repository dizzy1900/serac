"""serac's public output contract for downstream decision or financial layers.

`AvoidedLossRequest` carries a hazard forecast, the exposure it threatens and a set of
warning/intervention scenarios (always including the `none` baseline);
`AvoidedLossResponse` returns expected loss per scenario. The computation is not implemented
in Prompt 1: a response may be issued only with `status == "not_implemented"` and no losses.
`contract_version` is pinned to `"0.0.0"` until the schema is populated in Prompt 2.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from serac.domain.common import DOMAIN_CONFIG, Range, Slug
from serac.domain.events import AssetType
from serac.domain.forecast import CascadeForecast, ForecastModel

AVOIDED_LOSS_CONTRACT_VERSION: Final = "0.0.0"


class MoneyRange(BaseModel):
    """A monetary interval in a stated currency and price year."""

    model_config = DOMAIN_CONFIG

    low: float = Field(ge=0, allow_inf_nan=False)
    high: float = Field(ge=0, allow_inf_nan=False)
    best: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217")
    price_year: int = Field(ge=1900, le=2100)
    basis: str = Field(min_length=1, description="How the figure was derived")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low > self.high:
            raise ValueError(f"low={self.low} exceeds high={self.high}")
        if self.best is not None and not (self.low <= self.best <= self.high):
            raise ValueError(f"best={self.best} outside [{self.low}, {self.high}]")
        return self


class InterventionKind(StrEnum):
    none = "none"
    warning = "warning"
    evacuation = "evacuation"
    combined = "combined"


class WarningScenario(BaseModel):
    """One counterfactual: what warning or intervention is assumed."""

    model_config = DOMAIN_CONFIG

    scenario_id: Slug
    intervention: InterventionKind
    lead_time_min: Range | None = Field(
        default=None, description="Warning lead time; must be absent for the 'none' baseline"
    )
    description: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _baseline_has_no_lead_time(self) -> Self:
        if self.intervention == InterventionKind.none and self.lead_time_min is not None:
            raise ValueError(f"{self.scenario_id}: intervention=none cannot carry lead_time_min")
        if self.intervention != InterventionKind.none and self.lead_time_min is None:
            raise ValueError(
                f"{self.scenario_id}: intervention={self.intervention} needs lead_time_min"
            )
        return self


class ExposureItem(BaseModel):
    """One exposed asset as seen by the loss layer."""

    model_config = DOMAIN_CONFIG

    asset_id: Slug
    asset_type: AssetType
    transect_id: Slug | None = None
    replacement_value: MoneyRange | None = None
    population: Range | None = None


class AvoidedLossRequest(BaseModel):
    """Hazard footprint + arrival lead time per transect + exposure -> request for expected loss."""

    model_config = DOMAIN_CONFIG

    contract_version: Literal["0.0.0"] = AVOIDED_LOSS_CONTRACT_VERSION
    request_id: Slug
    requested_utc: AwareDatetime
    requester: str | None = None
    forecast: CascadeForecast
    exposure: list[ExposureItem] = Field(min_length=1)
    scenarios: list[WarningScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        if not any(s.intervention == InterventionKind.none for s in self.scenarios):
            problems.append("scenarios: must include one baseline with intervention='none'")
        ids = [s.scenario_id for s in self.scenarios]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"scenarios: scenario_id repeated {duplicates}")
        asset_ids = [e.asset_id for e in self.exposure]
        duplicates = sorted({i for i in asset_ids if asset_ids.count(i) > 1})
        if duplicates:
            problems.append(f"exposure: asset_id repeated {duplicates}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class AvoidedLossStatus(StrEnum):
    not_implemented = "not_implemented"
    computed = "computed"


class ScenarioLoss(BaseModel):
    """Expected loss under one scenario."""

    model_config = DOMAIN_CONFIG

    scenario_id: Slug
    expected_loss: MoneyRange
    expected_fatalities: Range | None = None
    avoided_vs_baseline: MoneyRange | None = None


class AvoidedLossResponse(BaseModel):
    """Expected loss with and without warning. `not_implemented` responses carry no numbers."""

    model_config = DOMAIN_CONFIG

    contract_version: Literal["0.0.0"] = AVOIDED_LOSS_CONTRACT_VERSION
    request_id: Slug
    status: AvoidedLossStatus
    computed_utc: AwareDatetime
    model: ForecastModel | None = None
    assumptions: list[str] = Field(min_length=1)
    losses: list[ScenarioLoss] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> Self:
        if self.status == AvoidedLossStatus.not_implemented and self.losses:
            raise ValueError("losses: must be empty when status=not_implemented")
        if self.status == AvoidedLossStatus.computed:
            if not self.losses:
                raise ValueError("losses: must be non-empty when status=computed")
            if self.model is None:
                raise ValueError("model: required when status=computed")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {
    "avoided-loss": AvoidedLossRequest,
    "avoided-loss-response": AvoidedLossResponse,
}
