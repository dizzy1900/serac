"""Landslide force-history contract (interface only).

The single-force inversion that produces a force history arrives in Prompt 2. This prompt
declares the shape so downstream contracts can reference it, and pins `status` to
`not_implemented` so no record can claim a force time series exists.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.seismic import Sncl

FORCE_HISTORY_CONTRACT_VERSION = "0.0.0"


class ForceHistory(BaseModel):
    """Time series of the net force a mass movement exerts on the ground.

    STUB (interface only, Prompt 1): every instance has `status == "not_implemented"` and no
    force samples. Prompt 2 replaces the `status` literal with `computed` and fills the arrays.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = FORCE_HISTORY_CONTRACT_VERSION
    status: Literal["not_implemented"] = "not_implemented"
    detection_id: str | None = Field(
        default=None, description="`DetectionCandidate.detection_id` that triggered inversion."
    )
    sncls: list[Sncl] = Field(default_factory=list, description="Channels the inversion used.")
    time_start_utc: AwareDatetime | None = None
    sample_interval_s: float | None = Field(default=None, gt=0)
    units: Literal["N"] = "N"
    force_north_n: None = Field(default=None, description="Reserved: north component samples.")
    force_east_n: None = Field(default=None, description="Reserved: east component samples.")
    force_up_n: None = Field(default=None, description="Reserved: vertical component samples.")
    inversion_method: str | None = None
    notes: str = Field(
        default="not implemented: single-force inversion is scheduled for Prompt 2",
        min_length=1,
    )

    @model_validator(mode="after")
    def _no_samples_when_not_implemented(self) -> Self:
        if self.status == "not_implemented" and any(
            component is not None
            for component in (self.force_north_n, self.force_east_n, self.force_up_n)
        ):
            raise ValueError("a not_implemented force history cannot carry samples")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"force-history": ForceHistory}
