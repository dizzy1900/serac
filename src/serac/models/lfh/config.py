"""Configuration for the force-history inversion, and the seal that stops it being tuned.

Every number the inversion depends on lives here, in one frozen pydantic model with a stable
hash. That is not tidiness: it is the anti-tuning mechanism. `validate-lfh` reproduces
published force histories under a config, `serac lfh seal` records that config's hash and the
git sha in `reports/m2/seal.json`, and the Langtang and Blatten runs are then required to
carry the same hash. If a knob is turned between the reproduction and the new-event run, the
gate says so.

The hash covers the config only. Fixture checksums and Green's-function checksums are checked
separately by the suite, so a changed input shows up as a different failure than a changed
knob.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.ports.greens import EarthModel

LFH_CONFIG_VERSION = "0.1.0"

#: Where the seal lives. The path is part of the contract with `validate-lfh`.
SEAL_PATH = Path("reports/m2/seal.json")


class BandConfig(BaseModel):
    """The long-period band the inversion works in.

    20-150 s is the classic single-force window: long enough that the source is effectively a
    point and the 1-D Earth model is adequate, short enough that a 30-200 s mass movement is
    resolved. It is also why 1 sps channels suffice, which is what makes the offline fixtures
    small enough to commit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    short_period_s: float = Field(default=20.0, gt=0)
    long_period_s: float = Field(default=150.0, gt=0)
    corners: int = Field(default=4, ge=2, le=8)
    zerophase: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.short_period_s >= self.long_period_s:
            raise ValueError("short_period_s must be shorter than long_period_s")
        return self


class GridConfig(BaseModel):
    """The gSF trial-location grid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    half_width_km: float = Field(default=10.0, gt=0)
    spacing_km: float = Field(default=2.0, gt=0)
    depths_m: tuple[float, ...] = (1000.0,)

    @property
    def n_per_side(self) -> int:
        return 2 * round(self.half_width_km / self.spacing_km) + 1


class RegularisationConfig(BaseModel):
    """Second-difference Tikhonov, with lambda taken from the L-curve corner.

    Zero endpoints are not a regularisation choice but a physical one: a mass movement starts
    at rest and ends at rest, so the net force it exerts is zero before it begins and after it
    stops. Enforcing that removes the ramp the inversion would otherwise be free to add.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: Literal[2] = 2
    zero_endpoints: bool = True
    lambda_min: float = Field(default=1e-4, gt=0)
    lambda_max: float = Field(default=1e4, gt=0)
    n_lambda: int = Field(default=40, ge=5)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.lambda_min >= self.lambda_max:
            raise ValueError("lambda_min must be below lambda_max")
        return self


class StationConfig(BaseModel):
    """Which channels the inversion is allowed to stand on, and when it must refuse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_distance_deg: float = Field(default=0.5, ge=0)
    max_distance_deg: float = Field(default=15.0, gt=0)
    max_stations: int = Field(default=12, ge=1)
    channels: tuple[str, ...] = ("LHZ", "LHN", "LHE", "LH1", "LH2")
    #: A location from a five-station, 250-degree-gap network is a fabricated number.
    min_stations: int = Field(default=5, ge=1)
    max_azimuthal_gap_deg: float = Field(default=200.0, gt=0, le=360)
    #: Traces whose band-passed amplitude is this many times the median are dropped as glitches.
    amplitude_outlier_factor: float = Field(default=20.0, gt=1)
    #: The model must explain at least this fraction of the data variance, or serac refuses.
    #:
    #: Geometry is not the only way an inversion can be unsupported. When the signal is not in
    #: the records at all, least squares still returns a smooth force history with a clean
    #: envelope -- and an amplitude set by noise rather than by the event. The synthetic
    #: round-trip in `tests/unit/models/lfh/test_inversion.py` shows exactly this: a
    #: deliberately broken inversion lands at VR = 0.11 and looks fine.
    #:
    #: A fifth of the variance is a low bar deliberately: it is meant to catch "there is no
    #: signal here", not to grade quality. It refuses **more** than the brief requires and can
    #: never make serac agree with a published number it would otherwise disagree with.
    min_variance_reduction: float = Field(default=0.20, ge=0, le=1)


class MassConfig(BaseModel):
    """Assumptions the two mass estimators are allowed to make, stated up front."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Basal friction as a **fraction of tan(theta)**, not an absolute coefficient.
    #:
    #: The apparent friction of a mass movement is its Heim ratio H/L = tan(theta): that is
    #: what the runout geometry measures. Basal friction must be *below* it during the
    #: acceleration phase or the mass would never move at all, so an absolute range like
    #: [0.10, 0.45] is wrong wherever tan(theta) < 0.45 -- it drives the effective
    #: acceleration to its floor and inflates the mass by an order of magnitude. Bingham
    #: Canyon, with H/L = 0.29, is exactly such a case.
    #:
    #: Parameterising mu = friction_ratio * tan(theta) makes the constraint structural:
    #: a_eff = g sin(theta) (1 - friction_ratio), which is positive for any slope by
    #: construction and reduces to the familiar Coulomb form.
    friction_ratio_min: float = Field(default=0.20, gt=0, lt=1)
    friction_ratio_max: float = Field(default=0.80, gt=0, lt=1)
    #: Bulk densities used only to convert a published *volume* into a mass for comparison.
    rock_density_kg_m3: float = Field(default=2700.0, gt=0)
    ice_density_kg_m3: float = Field(default=917.0, gt=0)
    gravity_m_s2: float = Field(default=9.81, gt=0)
    #: Path angles the DEM-free estimator is clamped to; outside this a slide is not a slide.
    min_path_angle_deg: float = Field(default=8.0, gt=0, lt=90)
    max_path_angle_deg: float = Field(default=55.0, gt=0, lt=90)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.friction_ratio_min >= self.friction_ratio_max:
            raise ValueError("friction_ratio_min must be below friction_ratio_max")
        if self.min_path_angle_deg >= self.max_path_angle_deg:
            raise ValueError("min_path_angle_deg must be below max_path_angle_deg")
        return self


class BootstrapConfig(BaseModel):
    """What the 5-95% intervals are resampled over."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    n_draws: int = Field(default=200, ge=20)
    seed: int = 20260903
    resample_stations: bool = True
    #: Multiplicative jitter applied to each band corner, drawn log-uniformly.
    band_jitter: float = Field(default=1.25, ge=1.0)
    #: Multiplicative jitter applied to lambda, drawn log-uniformly.
    lambda_jitter: float = Field(default=3.0, ge=1.0)
    depths_m: tuple[float, ...] = (500.0, 1000.0, 2000.0)


class LfhConfig(BaseModel):
    """Everything the inversion depends on, hashed into the seal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = LFH_CONFIG_VERSION
    earth_model: EarthModel = EarthModel.prem_a_20s
    dt_s: float = Field(default=1.0, gt=0)
    #: Data window, relative to the event origin time.
    window_before_s: float = Field(default=120.0, ge=0)
    window_after_s: float = Field(default=780.0, gt=0)
    #: Length of the force time series, and where it starts relative to origin.
    source_duration_s: float = Field(default=300.0, gt=0)
    source_lead_s: float = Field(default=60.0, ge=0)
    greens_duration_s: float = Field(default=900.0, gt=0)
    greens_step_deg: float = Field(default=0.05, gt=0)

    band: BandConfig = BandConfig()
    grid: GridConfig = GridConfig()
    regularisation: RegularisationConfig = RegularisationConfig()
    stations: StationConfig = StationConfig()
    mass: MassConfig = MassConfig()
    bootstrap: BootstrapConfig = BootstrapConfig()

    def canonical_json(self) -> str:
        """Key-sorted, whitespace-free JSON: the bytes the hash is taken over."""
        return json.dumps(json.loads(self.model_dump_json()), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def n_source_samples(self) -> int:
        return round(self.source_duration_s / self.dt_s) + 1

    @property
    def n_window_samples(self) -> int:
        return round((self.window_before_s + self.window_after_s) / self.dt_s) + 1

    @property
    def greens_shift_samples(self) -> int:
        """Green's-function index offset between the data window and the force series.

        Data sample `n` sits at `-window_before_s + n*dt` relative to origin; force sample `j`
        sits at `-source_lead_s + j*dt`. The Green's function must be evaluated at the elapsed
        time between them, so its index is `n - j - (window_before - source_lead)` and the
        offset is the negative of that lead difference.

        Getting this wrong is silent and expensive. Written once as
        `-(window_before + source_lead)` instead of `-(window_before - source_lead)`, it
        misaligned data and synthetics by 120 s. Nothing raised: the inversion returned a
        smooth force history with a well-formed L-curve and an honest-looking envelope, and a
        peak force two orders of magnitude too large, because amplitude is what least squares
        reaches for when it cannot match phase. The only visible symptom was the variance
        reduction falling to 0.11. `tests/unit/models/lfh/test_inversion.py` pins both the
        value and that symptom.
        """
        return -round((self.window_before_s - self.source_lead_s) / self.dt_s)


class Seal(BaseModel):
    """The sealed config: what the published reproductions were run under.

    A run against a new event carries its own config hash; `validate-lfh` compares it with
    this one and errors if they differ. The point is narrow and mechanical -- it cannot tell
    an honest change from a dishonest one, only that a change happened after the
    reproductions were validated, which is exactly when a change most needs explaining.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = LFH_CONFIG_VERSION
    sealed_at_utc: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    git_sha: str | None = None
    config: LfhConfig
    reproductions: list[str] = Field(
        default_factory=list, description="Event ids validated under this config before sealing."
    )
    notes: str = Field(
        default=(
            "Sealed after the published reproductions passed and before Langtang 2026 and "
            "Blatten 2025 were run. Any later change to the config invalidates the seal."
        ),
        min_length=1,
    )

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        actual = self.config.config_hash()
        if actual != self.config_hash:
            raise ValueError(f"seal config_hash {self.config_hash} does not hash the config")
        return self


def seal_config(
    config: LfhConfig, *, git_sha: str | None, reproductions: list[str], notes: str | None = None
) -> Seal:
    kwargs: dict[str, Any] = {
        "config_hash": config.config_hash(),
        "git_sha": git_sha,
        "config": config,
        "reproductions": sorted(reproductions),
    }
    if notes is not None:
        kwargs["notes"] = notes
    return Seal(**kwargs)


def write_seal(seal: Seal, repo: Path) -> Path:
    path = repo / SEAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(seal.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_seal(repo: Path) -> Seal | None:
    path = repo / SEAL_PATH
    if not path.exists():
        return None
    return Seal.model_validate_json(path.read_text(encoding="utf-8"))
