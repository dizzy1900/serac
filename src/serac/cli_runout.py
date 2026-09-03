"""`serac runout ...`: terrain, timing study, ensemble freeze/run, surrogate, validation.

Wire into `serac.cli` with:

    app.add_typer(cli_runout.app, name="runout", help="M4 runout ensemble and surrogate.")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from serac.models.runout.ensemble import (
    EnsembleDesign,
    design_from_payload,
    read_frozen_design,
    write_frozen,
)
from serac.models.runout.params import SOLVER_NAME, SOLVER_VERSION

app = typer.Typer(
    name="runout", help="M4: runout ensemble and neural surrogate.", no_args_is_help=True
)

RepoOpt = Annotated[Path, typer.Option("--repo", help="Repository root.")]
AoiOpt = Annotated[str, typer.Option("--aoi", help="AOI id.")]
ReportsOpt = Annotated[
    Path | None, typer.Option("--reports-dir", help="Defaults to reports/runout.")
]

DEFAULT_AOI = "lhende-khola-trishuli"


def _reports(repo: Path, reports_dir: Path | None) -> Path:
    return reports_dir or (repo / "reports" / "runout")


@app.command("terrain")
def terrain_command(
    repo: RepoOpt = Path("."),
    aoi: AoiOpt = DEFAULT_AOI,
    resolution_m: Annotated[float, typer.Option(help="Solver grid resolution.")] = 30.0,
) -> None:
    """Build and describe the conditioned corridor terrain at one resolution."""
    import numpy as np

    from serac.models.runout.corridor import load_frame, roundtrip_rms_px
    from serac.models.runout.terrain import corridor_terrain, thalweg_is_draining

    t = corridor_terrain(repo, aoi_id=aoi, resolution_m=resolution_m)
    summary = t.summary()
    draining, worst = thalweg_is_draining(t)
    summary["thalweg_draining"] = draining
    summary["thalweg_worst_rise_m"] = worst

    frame = load_frame(repo / "data" / "aoi" / aoi, t.grid.epsg)
    grid = t.grid
    xs = grid.x_min + grid.resolution_m * (np.arange(grid.width) + 0.5)
    ys = grid.y_max - grid.resolution_m * (np.arange(grid.height) + 0.5)
    xx, yy = np.meshgrid(xs, ys)
    rms, worst_px = roundtrip_rms_px(frame, xx[t.frame_valid], yy[t.frame_valid], grid.resolution_m)
    summary["frame_roundtrip_rms_px"] = rms
    summary["frame_roundtrip_max_px"] = worst_px
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


@app.command("study")
def study_command(
    repo: RepoOpt = Path("."),
    aoi: AoiOpt = DEFAULT_AOI,
    reports_dir: ReportsOpt = None,
    resolutions: Annotated[str, typer.Option(help="Comma-separated resolutions.")] = "90,60,30",
    max_time_s: Annotated[float, typer.Option(help="Simulated seconds per member.")] = 7200.0,
) -> None:
    """Timing and grid convergence. Run this **before** sizing the ensemble."""
    from serac.models.runout.study import run_study

    values = tuple(float(v) for v in resolutions.split(","))
    timing, convergence = run_study(
        repo,
        resolutions=values,
        aoi_id=aoi,
        reports_dir=_reports(repo, reports_dir),
        max_time_s=max_time_s,
    )
    typer.echo(f"timing:      {timing}")
    typer.echo(f"convergence: {convergence}")


@app.command("freeze")
def freeze_command(
    repo: RepoOpt = Path("."),
    reports_dir: ReportsOpt = None,
    members: Annotated[int, typer.Option(help="Total ensemble size.")] = 200,
    seed: Annotated[int, typer.Option(help="Latin-hypercube seed.")] = 20260903,
    blocks: Annotated[
        str, typer.Option(help="Resolution blocks as 'res:count,res:count'.")
    ] = "30:200",
    max_time_s: Annotated[float, typer.Option()] = 7200.0,
    notes: Annotated[str, typer.Option(help="Why this size, from measured cost.")] = "",
) -> None:
    """Freeze the ensemble design. Refuses to overwrite an existing freeze."""
    reports = _reports(repo, reports_dir)
    design = EnsembleDesign(
        n_members=members,
        seed=seed,
        resolutions=tuple(
            (float(part.split(":")[0]), int(part.split(":")[1])) for part in blocks.split(",")
        ),
        settings_template={
            "cfl": 0.45,
            "max_time_s": max_time_s,
            "output_interval_s": 60.0,
            "dry_depth_m": 0.02,
            "stop_kinetic_fraction": 1e-3,
        },
    )
    frozen = reports / "ENSEMBLE_FROZEN.md"
    if frozen.exists():
        existing = read_frozen_design(reports)
        if design_from_payload(existing).design_hash != design.design_hash:
            typer.echo(
                "refused: the ensemble is already frozen with a different design. "
                "Delete the freeze deliberately if you really mean to redesign it.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"already frozen: {design.design_hash}")
        return
    path = write_frozen(design, reports, notes or "No notes supplied.")
    typer.echo(f"frozen: {path}")
    typer.echo(f"design_hash: {design.design_hash}")
    typer.echo(f"solver: {SOLVER_NAME} v{SOLVER_VERSION}")


@app.command("run")
def run_command(
    repo: RepoOpt = Path("."),
    aoi: AoiOpt = DEFAULT_AOI,
    reports_dir: ReportsOpt = None,
    workers: Annotated[int | None, typer.Option(help="Processes; defaults to cores - 1.")] = None,
    limit: Annotated[
        int | None, typer.Option(help="Run at most this many pending members.")
    ] = None,
) -> None:
    """Run the frozen ensemble. Resume-safe: members already in the index are skipped."""
    from serac.models.runout.driver import run_ensemble

    reports = _reports(repo, reports_dir)
    design = design_from_payload(read_frozen_design(reports))
    index = run_ensemble(
        repo, design, aoi_id=aoi, reports_dir=reports, workers=workers, limit=limit
    )
    typer.echo(f"index: {index}")


@app.command("summarise")
def summarise_command(
    repo: RepoOpt = Path("."),
    reports_dir: ReportsOpt = None,
) -> None:
    """Print the ensemble's validity, flags, runout distribution and total bytes."""
    from serac.models.runout.summary import summarise_ensemble

    reports = _reports(repo, reports_dir)
    summary = summarise_ensemble(repo, reports)
    typer.echo(json.dumps(summary, indent=2, sort_keys=True, default=str))


@app.command("train")
def train_command(
    repo: RepoOpt = Path("."),
    aoi: AoiOpt = DEFAULT_AOI,
    reports_dir: ReportsOpt = None,
    epochs: Annotated[int, typer.Option()] = 300,
    device: Annotated[str, typer.Option(help="cpu or mps.")] = "cpu",
) -> None:
    """Train the corridor FNO and transect regressor; write `surrogate_metrics.json`."""
    from serac.models.runout.training import train_and_evaluate

    metrics, path = train_and_evaluate(
        repo,
        aoi_id=aoi,
        reports_dir=_reports(repo, reports_dir),
        epochs=epochs,
        device=device,
        progress=True,
    )
    typer.echo(json.dumps(metrics, indent=2, sort_keys=True))
    typer.echo(f"metrics: {path}")


@app.command("langtang")
def langtang_command(
    repo: RepoOpt = Path("."),
    aoi: AoiOpt = DEFAULT_AOI,
    reports_dir: ReportsOpt = None,
) -> None:
    """Write the Langtang sanity check: the closest member and the full mismatch distribution."""
    from serac.models.runout.langtang import write_sanity_check

    path = write_sanity_check(repo, aoi_id=aoi, reports_dir=_reports(repo, reports_dir))
    typer.echo(f"report: {path}")


@app.command("validate")
def validate_command(
    repo: RepoOpt = Path("."),
    reports_dir: Annotated[Path, typer.Option()] = Path("reports/validation"),
) -> None:
    """`make validate-runout`: gates, frozen hashes, disclaimers and vocabulary."""
    from serac.validation.result import print_result, write_report
    from serac.validation.runout import run_suite

    result = run_suite(repo)
    path = write_report(result, reports_dir)
    print_result(result)
    typer.echo(f"report: {path}")
    if not result.passed:
        raise typer.Exit(code=1)
