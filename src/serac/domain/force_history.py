"""Landslide force-history contract.

Prompt 1 declared the shape and pinned `status` to `not_implemented`. Prompt 2's single-force
inversion populates it, so the contract now carries the force time series with a 5-95%
envelope, the inverted source location, and a mass estimate.

Two rules are enforced by validators rather than by convention, because both are ways a
plausible-looking number could be published without the evidence behind it:

* a `computed` history must carry all nine force arrays of equal length, an envelope that
  actually brackets the median, a location and a variance reduction;
* `MassEstimate` is a strict interval, so a point mass cannot be constructed at all.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.detection import DetectionLocation
from serac.domain.seismic import Sncl

FORCE_HISTORY_CONTRACT_VERSION = "0.2.0"


class Interval(BaseModel):
    """A 5-95% interval with its median. Never collapse this to a single number."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p05: float
    p50: float
    p95: float
    units: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.p05 <= self.p50 <= self.p95:
            raise ValueError(f"interval must be ordered: {self.p05} <= {self.p50} <= {self.p95}")
        return self


class AEff(BaseModel):
    """The effective acceleration used to turn a peak force into a mass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value_m_s2: float = Field(gt=0)
    basis: Literal["dem_trajectory", "assumed_range"] = Field(
        description="How it was derived; `assumed_range` means no DEM path was available."
    )
    slope_deg: float | None = Field(default=None, ge=0, le=90)
    friction_coefficient: Interval | None = None
    notes: str | None = None


class MassEstimate(BaseModel):
    """Bulk mobilised mass as an interval, with every assumption named."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mass_kg_p05: float = Field(gt=0)
    mass_kg_p50: float = Field(gt=0)
    mass_kg_p95: float = Field(gt=0)
    method: Literal["fmax_over_aeff", "impulse_over_velocity", "combined"]
    a_eff: AEff | None = None
    consistency_ratio: float | None = Field(
        default=None,
        gt=0,
        description="M(estimator A) / M(estimator B); outside [1/3, 3] is reported, not hidden.",
    )
    assumptions: list[str] = Field(
        min_length=1, description="Every assumption behind the number, in words."
    )

    @model_validator(mode="after")
    def _strict_interval(self) -> Self:
        if not self.mass_kg_p05 < self.mass_kg_p50 < self.mass_kg_p95:
            raise ValueError(
                "mass must be a strict interval (p05 < p50 < p95): a point mass is not publishable"
            )
        return self


class GreensProvenance(BaseModel):
    """Which modelled Green's functions the inversion stood on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    earth_model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_url: str = Field(min_length=1)
    dt_s: float = Field(gt=0)
    band_s: tuple[float, float]
    cache_sha256: list[str] = Field(default_factory=list)
    modelled: Literal[True] = Field(
        default=True, description="Green's functions are modelled, never observed."
    )


class BootstrapInfo(BaseModel):
    """What the uncertainty was resampled over."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    n_draws: int = Field(gt=0)
    seed: int
    resampled: list[str] = Field(min_length=1)


class ForceHistory(BaseModel):
    """Time series of the net force a mass movement exerts on the ground."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = FORCE_HISTORY_CONTRACT_VERSION
    status: Literal["not_implemented", "computed", "failed"] = "not_implemented"
    detection_id: str | None = Field(
        default=None, description="`DetectionCandidate.detection_id` that triggered inversion."
    )
    event_id: str | None = None
    sncls: list[Sncl] = Field(default_factory=list, description="Channels the inversion used.")
    station_weights: dict[str, float] = Field(default_factory=dict)
    time_start_utc: AwareDatetime | None = None
    sample_interval_s: float | None = Field(default=None, gt=0)
    n_samples: int | None = Field(default=None, gt=0)
    units: Literal["N"] = "N"

    force_up_n: list[float] | None = None
    force_north_n: list[float] | None = None
    force_east_n: list[float] | None = None
    force_up_p05_n: list[float] | None = None
    force_up_p95_n: list[float] | None = None
    force_north_p05_n: list[float] | None = None
    force_north_p95_n: list[float] | None = None
    force_east_p05_n: list[float] | None = None
    force_east_p95_n: list[float] | None = None

    source_location: DetectionLocation | None = None
    variance_reduction: float | None = Field(default=None, ge=0, le=1)
    azimuthal_gap_deg: float | None = Field(default=None, ge=0, le=360)
    peak_force_n: Interval | None = None
    impulse_ns: Interval | None = None
    duration_s: Interval | None = None
    force_azimuth_deg: Interval | None = None
    mass: MassEstimate | None = None

    greens: GreensProvenance | None = None
    regularisation: str | None = None
    lambda_value: float | None = Field(default=None, gt=0)
    bootstrap: BootstrapInfo | None = None
    inversion_method: str | None = None
    notes: str = Field(
        default="not implemented: single-force inversion is scheduled for Prompt 2",
        min_length=1,
    )

    def _force_arrays(self) -> dict[str, list[float] | None]:
        return {
            name: getattr(self, name)
            for name in (
                "force_up_n",
                "force_north_n",
                "force_east_n",
                "force_up_p05_n",
                "force_up_p95_n",
                "force_north_p05_n",
                "force_north_p95_n",
                "force_east_p05_n",
                "force_east_p95_n",
            )
        }

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        arrays = self._force_arrays()
        if self.status != "computed":
            present = sorted(name for name, value in arrays.items() if value is not None)
            if present:
                raise ValueError(f"a {self.status} force history cannot carry samples: {present}")
            return self

        missing = sorted(name for name, value in arrays.items() if value is None)
        if missing:
            raise ValueError(f"a computed force history requires every array; missing {missing}")
        lengths = {len(value) for value in arrays.values() if value is not None}
        if len(lengths) != 1:
            raise ValueError(f"force arrays must share one length; got {sorted(lengths)}")
        (length,) = lengths
        if self.n_samples is not None and self.n_samples != length:
            raise ValueError(f"n_samples={self.n_samples} disagrees with array length {length}")
        for axis in ("up", "north", "east"):
            median = arrays[f"force_{axis}_n"]
            low = arrays[f"force_{axis}_p05_n"]
            high = arrays[f"force_{axis}_p95_n"]
            assert median is not None and low is not None and high is not None
            for i, (lo, mid, hi) in enumerate(zip(low, median, high, strict=True)):
                if not lo <= mid <= hi:
                    raise ValueError(
                        f"force_{axis} envelope must bracket the median at sample {i}: "
                        f"{lo} <= {mid} <= {hi}"
                    )
        for name in ("source_location", "variance_reduction", "mass", "greens"):
            if getattr(self, name) is None:
                raise ValueError(f"a computed force history requires {name}")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {
    "force-history": ForceHistory,
    "mass-estimate": MassEstimate,
}
