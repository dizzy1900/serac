"""Timing and grid convergence, run **before** the ensemble is sized.

`timing.json` records the measured wall clock of one member at each resolution, and the
ensemble size is then chosen against that number rather than an estimate. `grid_convergence.json`
runs the *same* parameter vector at 90 / 60 / 30 m and reports how much the answers move, which
is what tells you whether a 60 m member is worth keeping in the ensemble at all.

Convergence is reported on the quantities the surrogate and the cascade rules actually consume:
the runout distance along the corridor, the arrival time at each committed transect, and the
L1 difference of the binned max-depth profile. Reporting a "convergence rate" from three points
on a terrain-driven problem would be over-claiming, so the study reports **differences between
resolutions** and leaves the reader to judge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from serac.models.runout.corridor import TransectChainage, load_frame, transect_chainages
from serac.models.runout.params import SolverSettings, VoellmyParameters
from serac.models.runout.release import emplace_release
from serac.models.runout.runner import CorridorProfile, reduce_to_corridor
from serac.models.runout.solver import VoellmySolver
from serac.models.runout.terrain import CorridorTerrain, corridor_terrain

TIMING_FILENAME = "timing.json"
CONVERGENCE_FILENAME = "grid_convergence.json"

STUDY_PARAMETERS = VoellmyParameters(
    release_volume_m3=1.0e8,
    ice_fraction=0.5,
    release_elevation_band_m=(3800.0, 4600.0),
    entrainment_coefficient=0.01,
    mu=0.05,
    xi_m_s2=1000.0,
    critical_shear_pa=500.0,
)
"""One vector near the middle of the frozen design, used for every resolution in the study."""


@dataclass(frozen=True)
class ResolutionResult:
    resolution_m: float
    terrain: CorridorTerrain
    profile: CorridorProfile
    wall_time_s: float
    terrain_build_s: float
    steps: int
    simulated_time_s: float
    reach_m: float
    max_depth_m: float
    mass_relative_error: float
    active_cells: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution_m": self.resolution_m,
            "active_cells": self.active_cells,
            "terrain_build_s": round(self.terrain_build_s, 3),
            "solver_wall_s": round(self.wall_time_s, 3),
            "ms_per_step": round(1000.0 * self.wall_time_s / max(self.steps, 1), 4),
            "steps": self.steps,
            "simulated_time_s": round(self.simulated_time_s, 2),
            "reach_m": round(self.reach_m, 1),
            "max_depth_m": round(self.max_depth_m, 3),
            "mass_relative_error": self.mass_relative_error,
        }


def run_one(
    repo: Path,
    resolution_m: float,
    *,
    aoi_id: str = "lhende-khola-trishuli",
    parameters: VoellmyParameters = STUDY_PARAMETERS,
    max_time_s: float = 7200.0,
) -> ResolutionResult:
    """Run the study vector at one resolution and reduce it to the corridor."""
    import time as _time

    t0 = _time.perf_counter()
    terrain = corridor_terrain(repo, aoi_id=aoi_id, resolution_m=resolution_m)
    build = _time.perf_counter() - t0

    settings = SolverSettings(resolution_m=resolution_m, cfl=0.45, max_time_s=max_time_s)
    emplacement = emplace_release(terrain, parameters)
    solver = VoellmySolver(
        bed=np.asarray(terrain.elevation, dtype=np.float64),
        domain_mask=terrain.domain_mask,
        outflow_mask=terrain.outflow_mask,
        erodible_depth=np.asarray(terrain.erodible_depth, dtype=np.float64),
        parameters=parameters,
        settings=settings,
    )
    result = solver.run(emplacement.depth)
    profile = reduce_to_corridor(terrain, result)
    chainage = np.asarray(terrain.chainage_m, dtype=np.float64)
    wet = np.isfinite(result.arrival_time_s) & terrain.domain_mask
    return ResolutionResult(
        resolution_m=resolution_m,
        terrain=terrain,
        profile=profile,
        wall_time_s=result.wall_time_s,
        terrain_build_s=build,
        steps=result.steps,
        simulated_time_s=result.time_s,
        reach_m=float(chainage[wet].max()) if wet.any() else 0.0,
        max_depth_m=float(np.nanmax(result.max_depth)),
        mass_relative_error=result.mass_balance["relative_error"],
        active_cells=terrain.active_cells,
    )


def transect_arrivals(
    profile: CorridorProfile, transects: list[TransectChainage]
) -> dict[str, float | None]:
    """First arrival at each committed transect, from the binned corridor profile."""
    out: dict[str, float | None] = {}
    for transect in transects:
        idx = int(np.argmin(np.abs(profile.chainage_m - transect.frame_chainage_m)))
        value = profile.arrival_time_s[idx]
        out[transect.transect_id] = None if not np.isfinite(value) else float(value)
    return out


def compare(
    a: ResolutionResult, b: ResolutionResult, transects: list[TransectChainage]
) -> dict[str, Any]:
    """How far apart two resolutions are on the quantities that get consumed downstream."""
    depth_a, depth_b = a.profile.max_depth_m, b.profile.max_depth_m
    l1 = float(np.abs(depth_a - depth_b).sum())
    scale = float(np.abs(depth_a).sum() + np.abs(depth_b).sum()) / 2.0
    arrivals_a = transect_arrivals(a.profile, transects)
    arrivals_b = transect_arrivals(b.profile, transects)
    arrival_delta: dict[str, float | None] = {}
    for key in arrivals_a:
        va, vb = arrivals_a[key], arrivals_b[key]
        arrival_delta[key] = None if (va is None or vb is None) else round(vb - va, 1)
    both = (depth_a > 1.0) & (depth_b > 1.0)
    either = (depth_a > 1.0) | (depth_b > 1.0)
    return {
        "coarse_m": a.resolution_m,
        "fine_m": b.resolution_m,
        "reach_delta_m": round(b.reach_m - a.reach_m, 1),
        "reach_relative_delta": (
            round((b.reach_m - a.reach_m) / a.reach_m, 4) if a.reach_m > 0 else None
        ),
        "max_depth_delta_m": round(b.max_depth_m - a.max_depth_m, 3),
        "depth_profile_l1": round(l1, 3),
        "depth_profile_relative_l1": round(l1 / scale, 4) if scale > 0 else None,
        "inundation_iou_1m": round(float(both.sum() / either.sum()), 4) if either.any() else None,
        "arrival_delta_s": arrival_delta,
        "arrivals_coarse_s": arrivals_a,
        "arrivals_fine_s": arrivals_b,
    }


def run_study(
    repo: Path,
    *,
    resolutions: tuple[float, ...] = (90.0, 60.0, 30.0),
    aoi_id: str = "lhende-khola-trishuli",
    reports_dir: Path | None = None,
    max_time_s: float = 7200.0,
) -> tuple[Path, Path]:
    """Run the timing and grid-convergence study; write both reports; return their paths."""
    reports = reports_dir or (repo / "reports" / "runout")
    reports.mkdir(parents=True, exist_ok=True)

    results = [run_one(repo, r, aoi_id=aoi_id, max_time_s=max_time_s) for r in resolutions]
    frame = load_frame(repo / "data" / "aoi" / aoi_id, results[0].terrain.grid.epsg)
    transects = transect_chainages(repo / "data" / "aoi" / aoi_id, frame)

    timing = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "parameters": STUDY_PARAMETERS.model_dump(mode="json"),
        "max_time_s": max_time_s,
        "note": (
            "One member per resolution with the same parameter vector. The ensemble size is "
            "chosen against these measured numbers, not an estimate. Wall clock is single-core; "
            "the ensemble runs one member per process."
        ),
        "per_resolution": [r.as_dict() for r in results],
    }
    (reports / TIMING_FILENAME).write_text(
        json.dumps(timing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    convergence = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "parameters": STUDY_PARAMETERS.model_dump(mode="json"),
        "transects": [
            {
                "transect_id": t.transect_id,
                "declared_chainage_km": t.declared_chainage_km,
                "frame_chainage_m": round(t.frame_chainage_m, 1),
            }
            for t in transects
        ],
        "per_resolution": [r.as_dict() for r in results],
        "pairs": [compare(results[i], results[i + 1], transects) for i in range(len(results) - 1)],
        "note": (
            "Differences between resolutions, not a fitted convergence rate: three points on a "
            "terrain-driven problem do not support one."
        ),
    }
    (reports / CONVERGENCE_FILENAME).write_text(
        json.dumps(convergence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return reports / TIMING_FILENAME, reports / CONVERGENCE_FILENAME
