"""Resume-safe parallel execution of a frozen ensemble.

Members are independent, so the driver is a process pool over `RunoutRunner.run`. Three
properties matter more than speed:

* **Resume-safe.** A member whose `run.json` records the same `input_hash` is loaded from cache
  and not recomputed, so an interrupted ensemble restarts where it stopped. Killing the driver
  mid-member leaves that member's directory without a `run.json`, and the next pass redoes it.
* **Flagged members are kept.** A member that hit the simulated-time limit while still moving is
  information about the rheology, not a failure. `MemberOutcome.flag_reasons` travels into the
  index; only members whose *numbers* are untrustworthy are marked invalid.
* **Ledgered.** Every artifact of every member gets a `ManifestEntry`. The ledger is
  append-only and each worker opens it separately, so writes are line-buffered appends; the
  driver serialises them through the parent instead, by having workers return their outcome and
  appending here.

The terrain is rebuilt once per worker process per resolution and cached in a module-level
dictionary, because conditioning a 30 m corridor takes about 6 s and the workers are long-lived.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serac.models.runout.ensemble import EnsembleDesign
from serac.models.runout.params import SOLVER_VERSION, SolverSettings, VoellmyParameters
from serac.models.runout.runner import MemberOutcome, RunoutRunner
from serac.models.runout.terrain import CorridorTerrain, corridor_terrain

INDEX_FILENAME = "ensemble_index.jsonl"

_TERRAIN_CACHE: dict[tuple[str, float], CorridorTerrain] = {}


def cached_terrain(repo: Path, aoi_id: str, resolution_m: float) -> CorridorTerrain:
    """Conditioned terrain for one resolution, built once per process."""
    key = (aoi_id, resolution_m)
    if key not in _TERRAIN_CACHE:
        _TERRAIN_CACHE[key] = corridor_terrain(repo, aoi_id=aoi_id, resolution_m=resolution_m)
    return _TERRAIN_CACHE[key]


@dataclass(frozen=True)
class MemberTask:
    index: int
    run_id: str
    resolution_m: float
    parameters: VoellmyParameters
    settings: SolverSettings
    design_hash: str = ""


def _run_member(repo_str: str, aoi_id: str, task: MemberTask) -> tuple[int, dict[str, Any]]:
    """Worker entry point. Returns the index and a compact summary of the outcome."""
    repo = Path(repo_str)
    terrain = cached_terrain(repo, aoi_id, task.resolution_m)
    runner = RunoutRunner(repo, terrain, aoi_id=aoi_id)
    outcome = runner.run(task.run_id, task.parameters, task.settings)
    return task.index, summarise(task, outcome)


def summarise(task: MemberTask, outcome: MemberOutcome, design_hash: str = "") -> dict[str, Any]:
    """The row that goes into `ensemble_index.jsonl`.

    `solver_version` and `design_hash` are carried explicitly. The 230-member ensemble committed
    here predates those fields: for those rows the solver version survives only in the output
    path (`.../v0.2.0/<run_id>/`) and the design hash only in `ensemble_summary.json`, which is
    recoverable but not self-describing.
    """
    run_json = outcome.run_json
    results = run_json["results"]
    return {
        "index": task.index,
        "run_id": outcome.run_id,
        "input_hash": outcome.input_hash,
        "solver_version": run_json["solver"]["version"],
        "design_hash": task.design_hash,
        "resolution_m": task.resolution_m,
        "cached": outcome.cached,
        "valid": outcome.valid,
        "flag_reasons": outcome.flag_reasons,
        "parameters": run_json["parameters"],
        "mass_balance": run_json["mass_balance"],
        "results": results,
        "wall_time_s": run_json["timing"]["solver_wall_s"],
        "directory": Path(outcome.directory).as_posix(),
    }


def tasks_for(design: EnsembleDesign) -> list[MemberTask]:
    design_hash = design.design_hash
    return [
        MemberTask(
            index=index,
            run_id=run_id,
            resolution_m=settings.resolution_m,
            parameters=parameters,
            settings=settings,
            design_hash=design_hash,
        )
        for index, (run_id, parameters, settings) in enumerate(design.all_members())
    ]


def read_index(path: Path) -> dict[int, dict[str, Any]]:
    """Existing rows keyed by member index; later rows win."""
    if not path.exists():
        return {}
    rows: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            row = json.loads(text)
            rows[int(row["index"])] = row
    return rows


def run_ensemble(
    repo: Path,
    design: EnsembleDesign,
    *,
    aoi_id: str = "lhende-khola-trishuli",
    reports_dir: Path | None = None,
    workers: int | None = None,
    limit: int | None = None,
    progress: bool = True,
) -> Path:
    """Run every member not already in the index; return the index path."""
    reports = reports_dir or (repo / "reports" / "runout")
    reports.mkdir(parents=True, exist_ok=True)
    index_path = reports / INDEX_FILENAME
    done = read_index(index_path)

    pending = [t for t in tasks_for(design) if t.index not in done]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        return index_path

    n_workers = workers or max(1, (os.cpu_count() or 2) - 1)
    started = datetime.now(tz=UTC)
    completed = 0
    with (
        ProcessPoolExecutor(max_workers=n_workers) as pool,
        index_path.open("a", encoding="utf-8") as handle,
    ):
        futures = {
            pool.submit(_run_member, repo.as_posix(), aoi_id, task): task for task in pending
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                _, row = future.result()
            except Exception as exc:  # a member that crashes is recorded, not silently dropped
                row = {
                    "index": task.index,
                    "run_id": task.run_id,
                    "solver_version": SOLVER_VERSION,
                    "design_hash": task.design_hash,
                    "resolution_m": task.resolution_m,
                    "cached": False,
                    "valid": False,
                    "flag_reasons": [f"{type(exc).__name__}: {exc}"],
                    "parameters": task.parameters.model_dump(mode="json"),
                    "wall_time_s": 0.0,
                }
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()
            completed += 1
            if progress:
                elapsed = (datetime.now(tz=UTC) - started).total_seconds()
                rate = elapsed / max(completed, 1)
                remaining = (len(pending) - completed) * rate / max(n_workers, 1)
                print(  # noqa: T201
                    f"[{completed}/{len(pending)}] {row['run_id']} "
                    f"valid={row['valid']} {row.get('wall_time_s', 0.0):.1f}s "
                    f"eta~{remaining / 60:.1f} min",
                    flush=True,
                )
    return index_path


def iter_index(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            yield json.loads(text)
