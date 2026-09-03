"""Detection contract emitted on `serac.detections`.

Prompt 1 pinned `source_location` to `None` so no stage could attach a fabricated epicentre
before an inversion existed to justify one. Prompt 2 widens it to a `DetectionLocation`, but
keeps the intent: a location may only be attached by a method that actually inverted for it,
and a `probability` may only be published alongside the calibration that produced it. Both
rules are enforced by validators, not convention.

Contract 0.2.0 is additive on the wire: `sncl` stays the single highest-scoring channel, and
the full contributing set arrives in `sncls`/`contributing_stations`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.seismic import Sncl

DETECTION_CONTRACT_VERSION = "0.2.0"

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DetectionLocation(BaseModel):
    """A source location produced by an inversion, never by a single-channel trigger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    depth_km: float | None = None
    uncertainty_radius_km: float | None = Field(default=None, ge=0)
    method: Literal["gsf_grid_search"] = Field(
        description="The only method allowed to set a location; extend deliberately."
    )
    grid_spacing_km: float = Field(gt=0)
    variance_reduction: float = Field(
        ge=0, le=1, description="Fraction of data variance the best-fitting node explains."
    )
    azimuthal_gap_deg: float = Field(
        ge=0, le=360, description="Largest azimuthal gap in the contributing station set."
    )
    source_refs: list[str] = Field(default_factory=list)


class ContributingStation(BaseModel):
    """One station's part in a multi-station detection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sncl: Sncl
    distance_km: float | None = Field(default=None, ge=0)
    azimuth_deg: float | None = Field(default=None, ge=0, le=360)
    station_score: float | None = None
    components_used: list[str] = Field(default_factory=list)


class DetectionCandidate(BaseModel):
    """A candidate mass-movement signal.

    `is_stub` distinguishes the Prompt 1 placeholder from a trained detector; it defaults to
    False now that a real detector exists, and the stub sets it explicitly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = DETECTION_CONTRACT_VERSION
    detection_id: str = Field(min_length=1)
    sncl: Sncl = Field(description="Highest-scoring contributing channel.")
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
    source_location: DetectionLocation | None = Field(
        default=None,
        description="Null unless an inversion produced it; see the validator.",
    )
    sncls: list[Sncl] = Field(
        default_factory=list, description="Every contributing channel, including `sncl`."
    )
    contributing_stations: list[ContributingStation] = Field(default_factory=list)
    probability: float | None = Field(
        default=None, ge=0, le=1, description="Calibrated P(mass movement); needs a calibration."
    )
    probability_calibration: str | None = Field(
        default=None, description="How the probability was calibrated, e.g. `sigmoid`."
    )
    class_label: Literal["mass_movement", "tectonic", "noise"] | None = None
    class_probabilities: dict[str, float] = Field(default_factory=dict)
    model_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    is_stub: bool = Field(
        default=False, description="True only for the Prompt 1 placeholder detector."
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
        if self.probability is not None and not self.probability_calibration:
            raise ValueError(
                "probability requires probability_calibration: an uncalibrated score is not a "
                "probability and must not be published as one"
            )
        if self.source_location is not None and self.is_stub:
            raise ValueError("a stub detector must not attach a source location")
        for name, value in self.class_probabilities.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"class_probabilities[{name!r}] must lie in [0, 1]")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {
    "detection-candidate": DetectionCandidate,
    "detection-location": DetectionLocation,
}
