"""`RunoutRunner`: one member in, cached artifacts and a ledger row out.

Identity
--------
A member is identified by the sha256 of a canonical JSON document covering **everything that
can change a number**: the Voellmy parameters, the solver settings, `SOLVER_VERSION`, the AOI,
the resolution, and the sha256 of the conditioned terrain (which itself covers the DEM's
sha256, the corridor geometry and the conditioning constants). Re-running the same inputs
reads the cache and recomputes nothing; the outputs are byte-identical because the solver is
deterministic and the writers sort their keys and pin their compression.

Outputs per member, under `data/interim/runout/<aoi>/<run_id>/`
--------------------------------------------------------------
* `max_depth.tif`, `max_speed.tif`, `arrival_time.tif`, `deposit_depth.tif` -- deflate COGs on
  the solver grid, `provenance: derived`, `source: simulation_output`.
* `corridor.parquet` -- the 1-D chainage reduction the surrogate trains on.
* `run.json` -- parameters, mass balance, numerical flags, timings, and the input hash.

Every one of them gets a `ManifestEntry`. Nothing is written under `data/` without one.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from numpy.typing import NDArray

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.models.runout.params import (
    NOT_RAVAFLOW,
    RESOLUTION_LIMITATION,
    SINGLE_PHASE_LIMITATION,
    SOLVER_NAME,
    SOLVER_VERSION,
    SolverSettings,
    VoellmyParameters,
    stable_hash,
)
from serac.models.runout.release import (
    RELEASE_AT_REST_ASSUMPTION,
    RELEASE_BAND_ASSUMPTION,
    emplace_release,
)
from serac.models.runout.solver import RunResult, VoellmySolver
from serac.models.runout.terrain import (
    CONDITIONING_ASSUMPTION,
    ERODIBLE_ASSUMPTION,
    CorridorTerrain,
)
from serac.pipelines.grid import to_affine

F64 = NDArray[np.float64]

RUNOUT_LICENCE = (
    "Apache-2.0 (serac model output). Derived from Copernicus GLO-30 (see the dem_glo30 "
    "ledger rows for the DEM's own licence) and OpenStreetMap corridor geometry (ODbL)."
)
RASTER_NAMES = ("max_depth", "max_speed", "arrival_time", "deposit_depth")
CORRIDOR_BINS = 400
"""Chainage bins for the 1-D reduction: 250 m each over the 100 km corridor."""

BASE_ASSUMPTIONS: tuple[str, ...] = (
    NOT_RAVAFLOW,
    SINGLE_PHASE_LIMITATION,
    RESOLUTION_LIMITATION,
    CONDITIONING_ASSUMPTION,
    ERODIBLE_ASSUMPTION,
    RELEASE_AT_REST_ASSUMPTION,
    RELEASE_BAND_ASSUMPTION,
)


def terrain_fingerprint(terrain: CorridorTerrain) -> str:
    """Hash of everything about the ground that can move a number."""
    return stable_hash(
        {
            "dem_sha256": terrain.dem_sha256,
            "grid": terrain.grid.model_dump(mode="json"),
            "active_cells": terrain.active_cells,
            "outflow_cells": int(terrain.outflow_mask.sum()),
            "fill_volume_m3": round(terrain.fill_volume_m3, 6),
            "erodible_volume_m3": round(
                float(np.asarray(terrain.erodible_depth)[terrain.domain_mask].sum())
                * terrain.cell_area_m2,
                6,
            ),
        }
    )


def member_hash(
    parameters: VoellmyParameters, settings: SolverSettings, terrain_hash: str, aoi_id: str
) -> str:
    return stable_hash(
        {
            "solver": SOLVER_NAME,
            "solver_version": SOLVER_VERSION,
            "aoi_id": aoi_id,
            "terrain": terrain_hash,
            "parameters": parameters.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"),
        }
    )


@dataclass(frozen=True)
class CorridorProfile:
    """The 1-D reduction of a member: one row per chainage bin."""

    chainage_m: F64
    max_depth_m: F64
    max_speed_m_s: F64
    arrival_time_s: F64
    deposit_depth_m: F64
    wet_cells: NDArray[np.int64]
    bed_min_m: F64

    def to_table(self) -> pa.Table:
        columns: dict[str, Any] = {
            "chainage_m": pa.array(self.chainage_m),
            "max_depth_m": pa.array(self.max_depth_m),
            "max_speed_m_s": pa.array(self.max_speed_m_s),
            "arrival_time_s": pa.array(self.arrival_time_s),
            "deposit_depth_m": pa.array(self.deposit_depth_m),
            "wet_cells": pa.array(self.wet_cells),
            "bed_min_m": pa.array(self.bed_min_m),
        }
        return pa.table(columns)


def reduce_to_corridor(
    terrain: CorridorTerrain, result: RunResult, *, n_bins: int = CORRIDOR_BINS
) -> CorridorProfile:
    """Reduce the 2-D fields onto chainage bins.

    Uses only the inverse map `(x, y) -> s`, which is single-valued everywhere, so the corridor
    frame's non-invertible region (see `corridor.py`) does not affect this reduction. Depth and
    speed are binned as maxima and arrival as the minimum, because what matters downstream is
    the worst case reaching a chainage and the earliest time it did.
    """
    mask = terrain.domain_mask
    s = np.asarray(terrain.chainage_m, dtype=np.float64)[mask]
    edges = np.linspace(0.0, float(np.asarray(terrain.chainage_m)[mask].max()), n_bins + 1)
    idx = np.clip(np.searchsorted(edges, s, side="right") - 1, 0, n_bins - 1)

    depth = np.asarray(result.max_depth, dtype=np.float64)[mask]
    speed = np.asarray(result.max_speed, dtype=np.float64)[mask]
    arrival = np.asarray(result.arrival_time_s, dtype=np.float64)[mask]
    deposit = np.asarray(result.deposit_depth, dtype=np.float64)[mask]
    bed = np.asarray(terrain.elevation, dtype=np.float64)[mask]

    out_depth = np.zeros(n_bins)
    out_speed = np.zeros(n_bins)
    out_arrival = np.full(n_bins, np.nan)
    out_deposit = np.zeros(n_bins)
    out_wet = np.zeros(n_bins, dtype=np.int64)
    out_bed = np.full(n_bins, np.nan)

    order = np.argsort(idx, kind="stable")
    idx_sorted = idx[order]
    bounds = np.searchsorted(idx_sorted, np.arange(n_bins + 1))
    for b in range(n_bins):
        sel = order[bounds[b] : bounds[b + 1]]
        if sel.size == 0:
            continue
        out_bed[b] = bed[sel].min()
        d = depth[sel]
        out_depth[b] = d.max()
        out_speed[b] = speed[sel].max()
        out_deposit[b] = deposit[sel].max()
        a = arrival[sel]
        finite = a[np.isfinite(a)]
        if finite.size:
            out_arrival[b] = finite.min()
            out_wet[b] = int(finite.size)

    centres = 0.5 * (edges[:-1] + edges[1:])
    return CorridorProfile(
        chainage_m=centres,
        max_depth_m=out_depth,
        max_speed_m_s=out_speed,
        arrival_time_s=out_arrival,
        deposit_depth_m=out_deposit,
        wet_cells=out_wet,
        bed_min_m=out_bed,
    )


def write_raster(path: Path, data: F64, terrain: CorridorTerrain) -> None:
    """Deflate COG on the solver grid. Deterministic: fixed creation options, no overviews."""
    grid = terrain.grid
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="COG",
        dtype="float32",
        count=1,
        width=grid.width,
        height=grid.height,
        crs=f"EPSG:{grid.epsg}",
        transform=to_affine(grid),
        nodata=float("nan"),
        compress="deflate",
        predictor=3,
        level=9,
        blocksize=256,
        overviews="NONE",
    ) as dst:
        dst.write(np.asarray(data, dtype=np.float32), 1)


@dataclass(frozen=True)
class MemberOutcome:
    """Everything the ensemble driver needs to know about one member."""

    run_id: str
    input_hash: str
    directory: Path
    cached: bool
    valid: bool
    flag_reasons: list[str]
    run_json: dict[str, Any]

    @property
    def wall_time_s(self) -> float:
        return float(self.run_json["timing"]["solver_wall_s"])


class RunoutRunner:
    """Runs one member, caches by input hash, writes artifacts and ledger rows."""

    adapter_name = "runout_swe_voellmy"
    adapter_version = SOLVER_VERSION

    def __init__(
        self,
        repo: Path,
        terrain: CorridorTerrain,
        *,
        aoi_id: str = "lhende-khola-trishuli",
        data_dir: Path | None = None,
        ledger: JsonlManifestLedger | None = None,
    ) -> None:
        self.repo = repo
        self.terrain = terrain
        self.aoi_id = aoi_id
        self.data_dir = data_dir or (repo / "data")
        self.ledger = ledger or JsonlManifestLedger(self.data_dir / "manifest.jsonl")
        self.terrain_hash = terrain_fingerprint(terrain)

    def root(self) -> Path:
        return self.data_dir / "interim" / "runout" / self.aoi_id

    def member_dir(self, run_id: str) -> Path:
        return self.root() / run_id

    def assumptions(self, parameters: VoellmyParameters) -> list[str]:
        """The assumption strings that must travel with every number this member produces."""
        return [
            *BASE_ASSUMPTIONS,
            (
                f"Voellmy-Salm coefficients mu={parameters.mu:.4g} and "
                f"xi={parameters.xi_m_s2:.4g} m/s2 are sampled from the published range for "
                "rock-ice avalanches and debris flows, not fitted to any observation of this "
                "event."
            ),
            (
                f"Entrainment coefficient c_e={parameters.entrainment_coefficient:.4g} and "
                f"critical shear {parameters.critical_shear_pa:.4g} Pa are parameters of a "
                "closure, not measurements."
            ),
        ]

    def run(
        self,
        run_id: str,
        parameters: VoellmyParameters,
        settings: SolverSettings,
        *,
        force: bool = False,
        record_history: bool = True,
    ) -> MemberOutcome:
        """Run (or load from cache) one member and return its outcome."""
        input_hash = member_hash(parameters, settings, self.terrain_hash, self.aoi_id)
        directory = self.member_dir(run_id)
        run_json_path = directory / "run.json"

        if run_json_path.exists() and not force:
            existing = json.loads(run_json_path.read_text(encoding="utf-8"))
            if existing.get("input_hash") == input_hash and all(
                (directory / f"{n}.tif").exists() for n in RASTER_NAMES
            ):
                return MemberOutcome(
                    run_id=run_id,
                    input_hash=input_hash,
                    directory=directory,
                    cached=True,
                    valid=bool(existing["valid"]),
                    flag_reasons=list(existing["flag_reasons"]),
                    run_json=existing,
                )
            # stale: the inputs changed under this run_id, so the artifacts are not ours
            shutil.rmtree(directory, ignore_errors=True)

        emplacement = emplace_release(self.terrain, parameters)
        solver = VoellmySolver(
            bed=np.asarray(self.terrain.elevation, dtype=np.float64),
            domain_mask=self.terrain.domain_mask,
            outflow_mask=self.terrain.outflow_mask,
            erodible_depth=np.asarray(self.terrain.erodible_depth, dtype=np.float64),
            parameters=parameters,
            settings=settings,
        )
        started = datetime.now(tz=UTC)
        result = solver.run(emplacement.depth, record_history=record_history)
        profile = reduce_to_corridor(self.terrain, result)

        directory.mkdir(parents=True, exist_ok=True)
        fields = {
            "max_depth": result.max_depth,
            "max_speed": result.max_speed,
            "arrival_time": result.arrival_time_s,
            "deposit_depth": result.deposit_depth,
        }
        for name, data in fields.items():
            write_raster(
                directory / f"{name}.tif", np.asarray(data, dtype=np.float64), self.terrain
            )
        pq.write_table(
            profile.to_table(),
            directory / "corridor.parquet",
            compression="zstd",
            compression_level=9,
            version="2.6",
            write_statistics=False,
            store_schema=True,
        )

        chainage = np.asarray(self.terrain.chainage_m, dtype=np.float64)
        wet = np.isfinite(result.arrival_time_s) & self.terrain.domain_mask
        reach_m = float(chainage[wet].max()) if wet.any() else 0.0

        flag_reasons = list(result.flags.reasons)
        if emplacement.shortfall_fraction > 1e-6:
            flag_reasons.append(
                f"release short by {emplacement.shortfall_fraction:.1%} (depth cap reached)"
            )
        balance = result.mass_balance
        if abs(balance["relative_error"]) > 1e-6:
            flag_reasons.append(f"mass balance off by {balance['relative_error']:.2e}")
        fabricated = result.flags.repaired_volume_m3 / max(result.initial_volume_m3, 1.0)
        if fabricated > 1e-3:
            flag_reasons.append(f"positivity repairs fabricated {fabricated:.2%} of the release")

        run_json: dict[str, Any] = {
            "run_id": run_id,
            "input_hash": input_hash,
            "solver": {"name": SOLVER_NAME, "version": SOLVER_VERSION},
            "aoi_id": self.aoi_id,
            "terrain_hash": self.terrain_hash,
            "terrain": self.terrain.summary(),
            "parameters": parameters.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"),
            "emplacement": {
                "cells": emplacement.cells,
                "mean_depth_m": emplacement.mean_depth_m,
                "band_low_m": emplacement.band_low_m,
                "band_high_m": emplacement.band_high_m,
                "chainage_max_m": emplacement.chainage_max_m,
                "shortfall_fraction": emplacement.shortfall_fraction,
            },
            "mass_balance": balance,
            "numerical_flags": result.flags.as_dict(),
            "flag_reasons": flag_reasons,
            "valid": _is_valid(result, emplacement.shortfall_fraction),
            "results": {
                "steps": result.steps,
                "simulated_time_s": result.time_s,
                "reach_m": reach_m,
                "max_depth_m": float(np.nanmax(result.max_depth)),
                "max_speed_m_s": float(np.nanmax(result.max_speed)),
                "wet_cells": int(wet.sum()),
                "entrained_volume_m3": result.entrained_volume_m3,
                "outflow_volume_m3": result.outflow_volume_m3,
            },
            "history": result.history,
            "assumptions": self.assumptions(parameters),
            "timing": {
                "started_utc": started.isoformat(),
                "solver_wall_s": result.wall_time_s,
                "ms_per_step": 1000.0 * result.wall_time_s / max(result.steps, 1),
            },
        }
        run_json_path.write_text(
            json.dumps(run_json, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        self._ledger_rows(run_id, directory, input_hash, parameters, settings)
        return MemberOutcome(
            run_id=run_id,
            input_hash=input_hash,
            directory=directory,
            cached=False,
            valid=bool(run_json["valid"]),
            flag_reasons=flag_reasons,
            run_json=run_json,
        )

    def _ledger_rows(
        self,
        run_id: str,
        directory: Path,
        input_hash: str,
        parameters: VoellmyParameters,
        settings: SolverSettings,
    ) -> list[ManifestEntry]:
        """One `simulation_output` / `derived` row per artifact this member wrote."""
        now = datetime.now(tz=UTC)
        rows: list[ManifestEntry] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            rel = path.relative_to(self.repo).as_posix()
            rows.append(
                self.ledger.append(
                    ManifestEntry(
                        source=DataSource.simulation_output,
                        product_id=f"runout/{self.aoi_id}/{run_id}/{path.name}",
                        product_level=f"{SOLVER_NAME} v{SOLVER_VERSION}",
                        aoi_id=self.aoi_id,
                        path=rel,
                        sha256=sha256_of_file(path),
                        size_bytes=path.stat().st_size,
                        retrieved_at=now,
                        licence=RUNOUT_LICENCE,
                        provenance=Provenance.derived,
                        status=ManifestStatus.fetched,
                        adapter=self.adapter_name,
                        adapter_version=self.adapter_version,
                        params={
                            "input_hash": input_hash,
                            "terrain_hash": self.terrain_hash,
                            "resolution_m": settings.resolution_m,
                            "mu": parameters.mu,
                            "xi_m_s2": parameters.xi_m_s2,
                            "release_volume_m3": parameters.release_volume_m3,
                            "ice_fraction": parameters.ice_fraction,
                            "entrainment_coefficient": parameters.entrainment_coefficient,
                        },
                        notes=NOT_RAVAFLOW,
                    )
                )
            )
        return rows


def _is_valid(result: RunResult, shortfall: float) -> bool:
    """A member is valid unless something makes its numbers untrustworthy.

    Flagged-but-valid is the normal case and those members are retained: hitting the time limit
    while still moving is information about the rheology, not a failure. A member is invalid
    only if the mass balance does not close, if the positivity repairs fabricated a meaningful
    fraction of the release, or if the release could not be emplaced.
    """
    balance = result.mass_balance
    if not np.isfinite(balance["relative_error"]) or abs(balance["relative_error"]) > 1e-6:
        return False
    if result.flags.repaired_volume_m3 > 1e-3 * max(result.initial_volume_m3, 1.0):
        return False
    if shortfall > 1e-3:
        return False
    if result.flags.dt_floor_steps > 0:
        return False
    return bool(np.isfinite(result.max_depth).all())
