"""The Latin-hypercube ensemble: design, freeze, and a resume-safe driver.

The design is frozen **before** it is run, into `reports/runout/ENSEMBLE_FROZEN.md`, carrying
`SOLVER_VERSION` and a design hash. `validate-runout` recomputes both and fails if either has
moved. That is the mechanism that stops a design being quietly adjusted after seeing results:
changing any bound, the sample count, or the seed changes the hash, and changing the solver
changes the version.

Parameter ranges, and where they come from
------------------------------------------
* `release_volume_m3` 5e6 - 3e8: the range the brief specifies.
* `ice_fraction` 0.2 - 0.8: the brief's range. It enters a **single-phase** solver only through
  the mixture density, so it is a weak parameter here by construction, not by accident.
* `release_elevation_band_m`: a 400-1200 m band whose base is sampled between 3,600 and 4,600 m
  on the corridor profile.
* `entrainment_coefficient` 1e-4 - 3e-2, log-uniform: the closure in `solver.py` has no
  measured coefficient, so the range spans two and a half decades rather than pretending to a
  central value.
* `mu` 0.02 - 0.30, log-uniform, and `xi_m_s2` 200 - 3000, log-uniform: the published spread of
  Voellmy-Salm coefficients for rock-ice avalanches and debris flows. **These bounds were fixed
  from the literature range, not from the Langtang timings**, and the scoping runs recorded in
  `reports/runout/timing.json` show what that costs: most of this corridor's thalweg is too flat
  to sustain motion above mu ~ 0.08, so those members stop within about 15 km whatever else is
  varied. The measured share and threshold are in `reports/runout/terrain.json`
  (`thalweg_fraction_below_mu_threshold`) and are rendered by
  `serac.models.runout.terrain.thalweg_sentence`; they are deliberately not repeated here,
  because an earlier draft of this docstring quoted a figure at a different threshold from the
  one it was arguing about. That is a result about single-phase Voellmy rheology on this
  corridor, and it is reported as one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from serac.models.runout.params import (
    SOLVER_NAME,
    SOLVER_VERSION,
    SolverSettings,
    VoellmyParameters,
    stable_hash,
)

F64 = NDArray[np.float64]

FROZEN_FILENAME = "ENSEMBLE_FROZEN.md"
DESIGN_FILENAME = "ensemble_design.json"


@dataclass(frozen=True)
class ParameterRange:
    """One sampled dimension."""

    name: str
    low: float
    high: float
    log: bool = False

    def transform(self, unit: F64) -> F64:
        if self.log:
            scaled: F64 = np.exp(np.log(self.low) + unit * (np.log(self.high) - np.log(self.low)))
            return scaled
        linear: F64 = self.low + unit * (self.high - self.low)
        return linear

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "low": self.low, "high": self.high, "log": self.log}


DIMENSIONS: tuple[ParameterRange, ...] = (
    ParameterRange("release_volume_m3", 5.0e6, 3.0e8, log=True),
    ParameterRange("ice_fraction", 0.2, 0.8),
    ParameterRange("release_band_base_m", 3600.0, 4600.0),
    ParameterRange("release_band_width_m", 400.0, 1200.0),
    ParameterRange("entrainment_coefficient", 1.0e-4, 3.0e-2, log=True),
    ParameterRange("mu", 0.02, 0.30, log=True),
    ParameterRange("xi_m_s2", 200.0, 3000.0, log=True),
)

CRITICAL_SHEAR_PA = 500.0
"""Held fixed: with no measurement to sample against, varying it only widens the design."""


def latin_hypercube(n: int, dims: int, seed: int) -> F64:
    """Centred Latin hypercube on the unit cube. Deterministic in `(n, dims, seed)`."""
    rng = np.random.default_rng(seed)
    out = np.empty((n, dims), dtype=np.float64)
    for d in range(dims):
        perm = rng.permutation(n)
        out[:, d] = (perm + 0.5) / n
    return out


@dataclass(frozen=True)
class EnsembleDesign:
    """A frozen ensemble: the sample matrix plus everything that identifies it."""

    n_members: int
    seed: int
    resolutions: tuple[tuple[float, int], ...]
    """`(resolution_m, count)` pairs; counts must sum to `n_members`."""
    settings_template: dict[str, Any]
    dimensions: tuple[ParameterRange, ...] = DIMENSIONS
    critical_shear_pa: float = CRITICAL_SHEAR_PA

    def __post_init__(self) -> None:
        total = sum(count for _, count in self.resolutions)
        if total != self.n_members:
            raise ValueError(f"resolutions sum to {total}, not n_members={self.n_members}")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "solver": SOLVER_NAME,
            "solver_version": SOLVER_VERSION,
            "n_members": self.n_members,
            "seed": self.seed,
            "resolutions": [list(r) for r in self.resolutions],
            "dimensions": [d.as_dict() for d in self.dimensions],
            "critical_shear_pa": self.critical_shear_pa,
            "settings_template": self.settings_template,
        }

    @property
    def design_hash(self) -> str:
        return stable_hash(self.payload)

    def samples(self) -> F64:
        return latin_hypercube(self.n_members, len(self.dimensions), self.seed)

    def resolution_for(self, index: int) -> float:
        """Which resolution member `index` runs at; blocks are contiguous and deterministic."""
        cursor = 0
        for resolution, count in self.resolutions:
            if index < cursor + count:
                return resolution
            cursor += count
        raise IndexError(index)

    def member(self, index: int) -> tuple[str, VoellmyParameters, SolverSettings]:
        """`(run_id, parameters, settings)` for member `index`."""
        unit = self.samples()[index]
        values = {
            d.name: float(d.transform(np.asarray(unit[i]))) for i, d in enumerate(self.dimensions)
        }
        base = values["release_band_base_m"]
        width = values["release_band_width_m"]
        parameters = VoellmyParameters(
            release_volume_m3=values["release_volume_m3"],
            ice_fraction=values["ice_fraction"],
            release_elevation_band_m=(base, base + width),
            entrainment_coefficient=values["entrainment_coefficient"],
            mu=values["mu"],
            xi_m_s2=values["xi_m_s2"],
            critical_shear_pa=self.critical_shear_pa,
        )
        resolution = self.resolution_for(index)
        settings = SolverSettings.model_validate(
            {**self.settings_template, "resolution_m": resolution}
        )
        run_id = f"m{index:04d}-r{int(resolution):03d}"
        return run_id, parameters, settings

    def all_members(self) -> list[tuple[str, VoellmyParameters, SolverSettings]]:
        unit = self.samples()
        out: list[tuple[str, VoellmyParameters, SolverSettings]] = []
        for index in range(self.n_members):
            values = {
                d.name: float(d.transform(np.asarray(unit[index][i])))
                for i, d in enumerate(self.dimensions)
            }
            base = values["release_band_base_m"]
            parameters = VoellmyParameters(
                release_volume_m3=values["release_volume_m3"],
                ice_fraction=values["ice_fraction"],
                release_elevation_band_m=(base, base + values["release_band_width_m"]),
                entrainment_coefficient=values["entrainment_coefficient"],
                mu=values["mu"],
                xi_m_s2=values["xi_m_s2"],
                critical_shear_pa=self.critical_shear_pa,
            )
            resolution = self.resolution_for(index)
            settings = SolverSettings.model_validate(
                {**self.settings_template, "resolution_m": resolution}
            )
            out.append((f"m{index:04d}-r{int(resolution):03d}", parameters, settings))
        return out


def write_frozen(design: EnsembleDesign, reports_dir: Path, notes: str) -> Path:
    """Write `ENSEMBLE_FROZEN.md` and the machine-readable design beside it."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / DESIGN_FILENAME).write_text(
        json.dumps(design.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = "\n".join(
        f"| `{d.name}` | {d.low:g} | {d.high:g} | {'log-uniform' if d.log else 'uniform'} |"
        for d in design.dimensions
    )
    blocks = "\n".join(
        f"| {resolution:g} m | {count} |" for resolution, count in design.resolutions
    )
    text = f"""# Runout ensemble — FROZEN

Frozen {datetime.now(tz=UTC).date().isoformat()}. **Do not edit.** `validate-runout` recomputes
the design hash and the solver version and fails if either has moved, so an edit here
invalidates the ensemble rather than quietly redefining it.

| Field | Value |
|---|---|
| Solver | `{SOLVER_NAME}` |
| `SOLVER_VERSION` | `{SOLVER_VERSION}` |
| Design hash | `{design.design_hash}` |
| Members | {design.n_members} |
| Latin-hypercube seed | {design.seed} |

## Resolution blocks

| Resolution | Members |
|---|---|
{blocks}

## Sampled dimensions

| Parameter | Low | High | Sampling |
|---|---|---|---|
{rows}

`critical_shear_pa` is held at {design.critical_shear_pa:g} Pa.

## Solver settings

```json
{json.dumps(design.settings_template, indent=2, sort_keys=True)}
```

## Notes

{notes}

---

{_disclaimer()}
"""
    path = reports_dir / FROZEN_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def _disclaimer() -> str:
    from serac.models.runout.params import (
        NOT_RAVAFLOW,
        RESOLUTION_LIMITATION,
        SINGLE_PHASE_LIMITATION,
    )

    return "\n\n".join(
        (
            f"> **{NOT_RAVAFLOW}**",
            f"> {SINGLE_PHASE_LIMITATION}",
            f"> {RESOLUTION_LIMITATION}",
        )
    )


def read_frozen_design(reports_dir: Path) -> dict[str, Any]:
    path = reports_dir / DESIGN_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; the ensemble has not been frozen")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def design_from_payload(payload: dict[str, Any]) -> EnsembleDesign:
    """Rebuild a design from its frozen payload, so the hash can be recomputed independently."""
    return EnsembleDesign(
        n_members=int(payload["n_members"]),
        seed=int(payload["seed"]),
        resolutions=tuple((float(r), int(c)) for r, c in payload["resolutions"]),
        settings_template=dict(payload["settings_template"]),
        dimensions=tuple(
            ParameterRange(
                name=str(d["name"]), low=float(d["low"]), high=float(d["high"]), log=bool(d["log"])
            )
            for d in payload["dimensions"]
        ),
        critical_shear_pa=float(payload["critical_shear_pa"]),
    )
