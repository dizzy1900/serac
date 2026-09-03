"""Parameters, versions and the disclaimer text for the M4 runout lane.

`SOLVER_VERSION` is frozen into `reports/runout/ENSEMBLE_FROZEN.md` and re-asserted by
`validate-runout`. Bumping it invalidates the frozen ensemble by design: a solver change that
does not change the version would silently mix physics across members.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOLVER_NAME = "serac-swe-voellmy"
SOLVER_VERSION = "0.2.0"
"""Bumping this invalidates every cached run and the frozen ensemble design."""

RUNOUT_CONTRACT_VERSION = "0.1.0"

NOT_RAVAFLOW = (
    "NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy "
    f"v{SOLVER_VERSION}, a single-phase depth-averaged Voellmy-Salm solver implemented in this "
    "repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); "
    "cross-validation against r.avaflow is outstanding."
)
"""The disclaimer that must appear in the model card, every runout report and every forecast."""

SINGLE_PHASE_LIMITATION = (
    "Single-phase: the solver cannot represent two- or three-phase physics or phase separation "
    "between rock, ice and fluid. Ice melt, pore-pressure evolution and fluidisation are "
    "subsumed into the Voellmy coefficients, not resolved."
)

RESOLUTION_LIMITATION = (
    "30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two "
    "cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming "
    "numbers derived from deposit depth against channel geometry are order-of-magnitude "
    "indicators, not engineering estimates."
)

GRAVITY = 9.80665
"""m/s^2, standard gravity."""

ICE_DENSITY = 917.0
ROCK_DENSITY = 2650.0
WATER_DENSITY = 1000.0


class VoellmyParameters(BaseModel):
    """The physical parameters of one runout member.

    `mu` (dimensionless) is the Coulomb friction coefficient and `xi` (m/s^2) the turbulent
    Voellmy coefficient; together they give a terminal velocity `sqrt(xi h (sin t - mu cos t))`
    on a slope `t`. `entrainment_coefficient` is the dimensionless `c_e` of the erosion law
    documented in `solver.py`. None of these are calibrated to any observation; their ranges are
    the published spread for rock-ice avalanches and debris flows and are recorded as such.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_volume_m3: float = Field(gt=0.0, allow_inf_nan=False)
    ice_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    release_elevation_band_m: tuple[float, float]
    entrainment_coefficient: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    mu: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    xi_m_s2: float = Field(gt=0.0, allow_inf_nan=False)
    critical_shear_pa: float = Field(default=1000.0, ge=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _band(self) -> Self:
        low, high = self.release_elevation_band_m
        if not (low < high):
            raise ValueError(f"release_elevation_band_m: low={low} must be below high={high}")
        return self

    @property
    def bulk_density(self) -> float:
        """Mixture density from the ice fraction; the solver is single-phase and uses only this."""
        return self.ice_fraction * ICE_DENSITY + (1.0 - self.ice_fraction) * ROCK_DENSITY


class SolverSettings(BaseModel):
    """Numerical settings. Separate from the physics so a grid study varies only these."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_m: float = Field(default=30.0, gt=0.0, allow_inf_nan=False)
    cfl: float = Field(default=0.45, gt=0.0, le=0.9, allow_inf_nan=False)
    max_time_s: float = Field(default=7200.0, gt=0.0, allow_inf_nan=False)
    max_steps: int = Field(default=400_000, ge=1)
    dry_depth_m: float = Field(default=0.02, gt=0.0, allow_inf_nan=False)
    min_dt_s: float = Field(default=1e-4, gt=0.0, allow_inf_nan=False)
    output_interval_s: float = Field(default=30.0, gt=0.0, allow_inf_nan=False)
    max_velocity_m_s: float = Field(default=120.0, gt=0.0, allow_inf_nan=False)
    window_refresh_steps: int = Field(default=8, ge=1)
    stop_when_dry: bool = True
    stop_kinetic_fraction: float = Field(default=1e-3, ge=0.0, le=1.0, allow_inf_nan=False)


def stable_hash(payload: Any) -> str:
    """sha256 of a canonical JSON rendering; the identity used for caching and freezing."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
