"""Detection contract emitted on `serac.detections`.

In this prompt the only producer is a placeholder long-period/short-period energy-ratio stage
(`is_stub` is `True`). A detection never carries a source location: `source_location` is typed
as `None`, so no stage can attach a fabricated epicentre until an inversion exists to justify
one (Prompt 2 will widen the type when `ForceHistory` is implemented).
"""

from __future__ import annotations

from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.seismic import Sncl

DETECTION_CONTRACT_VERSION = "0.1.0"


class DetectionCandidate(BaseModel):
    """A candidate long-period signal on one channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = DETECTION_CONTRACT_VERSION
    detection_id: str = Field(min_length=1)
    sncl: Sncl
    detector: str = Field(min_length=1, description="Detector name, e.g. `lp-sp-ratio-stub`.")
    detector_version: str = Field(min_length=1)
    window_start_utc: AwareDatetime
    window_end_utc: AwareDatetime
    detected_at_stream_utc: AwareDatetime = Field(
        description="Stream time at which the detector fired (end of the triggering window)."
    )
    score: float = Field(description="Detector statistic, e.g. LP/SP energy ratio.")
    threshold: float = Field(description="Threshold the score exceeded.")
    features: dict[str, float] = Field(
        default_factory=dict, description="Named intermediate quantities, for the report."
    )
    source_location: None = Field(
        default=None,
        description="Always null in this prompt: no location is inferred from a single channel.",
    )
    is_stub: bool = Field(
        default=True, description="True while the detector is the Prompt 1 placeholder."
    )
    input_trace_ids: list[str] = Field(
        default_factory=list, description="`SeismicTrace.trace_id`s in the triggering window."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.window_end_utc < self.window_start_utc:
            raise ValueError("window_end_utc must not precede window_start_utc")
        if not self.window_start_utc <= self.detected_at_stream_utc <= self.window_end_utc:
            raise ValueError("detected_at_stream_utc must fall within the window")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"detection-candidate": DetectionCandidate}
