"""Slope-watch state: the L0/L1 output.

A watch state ranks a slope unit by how anomalous its recent kinematics are. It is
deliberately impoverished compared with what a reader might want:

* the tier is ordinal (Quiet / Elevated / Watch), not a probability, because with one
  positive event in the record there is nothing to calibrate a probability against;
* `score` is a robust z-score, and the field name says so rather than implying a likelihood;
* there is no field, anywhere, for a failure date or a probability of failure. That is not an
  oversight to be corrected later: serac does not make that claim, and `validate-watch` greps
  the exported schema and the reports to keep it that way.

`insufficient_data` is a first-class tier rather than a null, because "this slope could not
be measured" and "this slope looks quiet" are different statements and conflating them is how
an unobservable slope comes to look safe.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from serac.domain.common import DOMAIN_CONFIG, Slug

WATCH_CONTRACT_VERSION = "0.1.0"

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class WatchTier(StrEnum):
    """Ordinal state of a slope unit. Never a probability, never a date."""

    quiet = "quiet"
    elevated = "elevated"
    watch = "watch"
    insufficient_data = "insufficient_data"


class WatchInsufficientReason(StrEnum):
    """Why a unit could not be assessed. Each is a measurement, not a guess."""

    outside_footprint = "outside_footprint"
    low_los_sensitivity = "low_los_sensitivity"
    too_few_samples = "too_few_samples"
    low_coherence = "low_coherence"
    too_little_history = "too_little_history"


class SlopeWatchState(BaseModel):
    """One slope unit's tier at one moment, with the evidence behind it."""

    model_config = DOMAIN_CONFIG

    contract_version: str = WATCH_CONTRACT_VERSION
    aoi_id: Slug
    unit_id: Slug
    as_of_utc: AwareDatetime
    tier: WatchTier
    score: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Robust z-score. NOT a probability and NOT calibrated to any failure rate.",
    )
    score_basis: Literal["robust_z_min_temporal_spatial"] = "robust_z_min_temporal_spatial"
    los_velocity_mm_yr: float | None = Field(default=None, allow_inf_nan=False)
    los_acceleration_mm_yr2: float | None = Field(default=None, allow_inf_nan=False)
    los_sensitivity_signed: float | None = Field(default=None, ge=-1.0, le=1.0)
    n_samples: int = Field(ge=0)
    median_coherence: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_reason: WatchInsufficientReason | None = None
    method_id: str = Field(min_length=1)
    preregistration_sha256: str = Field(pattern=SHA256_PATTERN)
    notes: str | None = None

    @model_validator(mode="after")
    def _tier_and_evidence_agree(self) -> Self:
        unmeasurable = self.tier == WatchTier.insufficient_data
        if unmeasurable and self.score is not None:
            raise ValueError("an insufficient_data unit has no score; it was not assessed")
        if not unmeasurable and self.score is None:
            raise ValueError(f"tier={self.tier.value} requires a score")
        if unmeasurable and self.insufficient_reason is None:
            raise ValueError("insufficient_data requires a measured reason, not a bare null")
        if not unmeasurable and self.insufficient_reason is not None:
            raise ValueError(
                f"tier={self.tier.value} was assessed, so it cannot carry an insufficient_reason"
            )
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"slope-watch-state": SlopeWatchState}
