"""Regenerate `reports/runout/verification.json` from the solver verification cases.

Committed so the file has a generator in the tree: an earlier `verification.json` was written by
an ad-hoc script that was never committed, and it kept a `solver_version` stamp of 0.1.0 through
the bump to 0.2.0. Every case here is the same one asserted in
`tests/unit/models/test_runout_solver.py`; this script records the numbers, the tests gate them.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "unit" / "models"))

from test_runout_solver import (
    PRODUCTION_CFL,
    _terminal_velocity_error,
    make_parameters,
)

from serac.models.runout.params import (
    GRAVITY,
    SOLVER_NAME,
    SOLVER_VERSION,
    SolverSettings,
)
from serac.models.runout.solver import VoellmySolver, ritter_solution

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
CFL_GRID = (PRODUCTION_CFL, 0.4, 0.2, 0.1, 0.05)


def ritter(n_cells: int) -> dict[str, float]:
    length, depth, t_end = 1000.0, 10.0, 12.0
    dx = length / n_cells
    shape = (3, n_cells)
    solver = VoellmySolver(
        bed=np.zeros(shape),
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=1e-9, xi_m_s2=1e12),
        settings=SolverSettings(
            resolution_m=dx,
            cfl=0.45,
            max_time_s=t_end,
            dry_depth_m=1e-4,
            stop_when_dry=False,
            stop_kinetic_fraction=0.0,
        ),
    )
    centres = (np.arange(n_cells) + 0.5) * dx - length / 2.0
    h0 = np.where(centres < 0.0, depth, 0.0)[None, :] * np.ones((shape[0], 1))
    result = solver.run(h0)
    analytic, _ = ritter_solution(centres, result.time_s, depth)
    c0 = math.sqrt(GRAVITY * depth)
    inside = (centres > -0.9 * c0 * result.time_s) & (centres < 2.2 * c0 * result.time_s)
    numeric = result.final_depth[1]
    l1 = float(np.abs(numeric[inside] - analytic[inside]).sum() * dx)
    scale = float(np.abs(analytic[inside]).sum() * dx)
    return {
        "cells": n_cells,
        "dx_m": dx,
        "l1_m2": round(l1, 4),
        "l1_relative": round(l1 / scale, 5),
    }


def mass_conservation() -> dict[str, float]:
    shape = (24, 48)
    rng = np.random.default_rng(11)
    bed = 0.02 * np.arange(shape[1])[None, :] * np.ones((shape[0], 1))
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(),
        settings=SolverSettings(resolution_m=10.0, cfl=0.45, max_time_s=40.0, stop_when_dry=False),
    )
    h0 = np.zeros(shape)
    h0[8:16, 4:12] = 3.0 + rng.uniform(0.0, 0.5, size=(8, 8))
    expected = float(h0.sum() * solver.cell_area)
    result = solver.run(h0)
    # signed, so the direction of the residual is not lost
    return {
        "expected_m3": expected,
        "final_m3": result.final_volume_m3,
        "signed_relative_error": (result.final_volume_m3 - expected) / expected,
        "absolute_relative_error": abs(result.final_volume_m3 - expected) / expected,
    }


def lake_at_rest() -> dict[str, float]:
    shape = (20, 40)
    bed = np.random.default_rng(3).uniform(0.0, 4.0, size=shape)
    level = 6.0
    h0 = np.maximum(level - bed, 0.0)
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(),
        settings=SolverSettings(resolution_m=25.0, cfl=0.45, max_time_s=200.0, stop_when_dry=False),
    )
    result = solver.run(h0)
    wet = result.final_depth > 1e-9
    return {
        "steps": result.steps,
        "max_surface_deviation_m": float(np.abs((result.final_depth + bed)[wet] - level).max()),
        "max_speed_m_s": float(result.max_speed.max()),
    }


def main() -> None:
    errors = {f"{c:g}": round(_terminal_velocity_error(c), 5) for c in CFL_GRID}
    payload = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "generator": "scripts/runout_verification_report.py",
        "solver": SOLVER_NAME,
        "solver_version": SOLVER_VERSION,
        "note": (
            "Every case here is also asserted in tests/unit/models/test_runout_solver.py; this "
            "file records the numbers, the tests gate them."
        ),
        "mass_conservation_closed_domain": mass_conservation(),
        "lake_at_rest": lake_at_rest(),
        "ritter_dam_break": [ritter(n) for n in (100, 200, 400)],
        "production_cfl": PRODUCTION_CFL,
        "voellmy_terminal_velocity_relative_error_by_cfl": errors,
        "voellmy_terminal_velocity_at_production_cfl": errors[f"{PRODUCTION_CFL:g}"],
        "voellmy_terminal_velocity_note": (
            "Gravity and friction are applied as separate operators within a step, so the "
            f"balance is recovered only to first order in dt. At the production CFL of "
            f"{PRODUCTION_CFL:g} the settled speed sits "
            f"{errors[f'{PRODUCTION_CFL:g}'] * 100:.1f}% below the analytic value, which biases "
            "every modelled arrival time late. An earlier version of this file quoted the "
            "CFL 0.4 figure as though it were the production one, understating the bias. "
            "Reported, not removed."
        ),
    }
    path = REPO / "reports" / "runout" / "verification.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")  # noqa: T201
    summary = {
        k: payload[k]
        for k in ("solver_version", "production_cfl", "voellmy_terminal_velocity_at_production_cfl")
    }
    print(json.dumps(summary, indent=1))  # noqa: T201


if __name__ == "__main__":
    main()
