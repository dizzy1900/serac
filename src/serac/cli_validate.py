"""`serac validate <suite>`, `serac validate stamp`, and `serac promote`.

Each suite lives in `serac.validation.<suite>` and exposes `run_suite(repo, ...)`. A suite
that is not implemented yet fails loudly here rather than passing silently.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from serac.validation.promote import (
    REQUIRED_SUITES,
    PromotionRefusedError,
    make_stamp,
    promote,
    write_stamp,
)
from serac.validation.result import SuiteResult, print_result, write_report

app = typer.Typer(name="validate", help="Validation harness suites.", no_args_is_help=True)

REPO_OPTION = typer.Option(Path("."), "--repo", help="Repository root.")
REPORTS_OPTION = typer.Option(
    Path("reports/validation"), "--reports-dir", help="Where suite reports are written."
)
CUBE_PATH_OPTION = typer.Option(
    None, "--path", help="Cube to validate; default builds chamoli-rishiganga from fixtures."
)
REPLAY_DIR_OPTION = typer.Option(
    Path("reports/replay"), "--report-dir", help="Where replay reports are written."
)
PROMOTIONS_OPTION = typer.Option(Path("reports/promotion"), "--promotions-dir")


def _load_runner(name: str) -> Callable[..., SuiteResult]:
    try:
        module = importlib.import_module(f"serac.validation.{name}")
    except ModuleNotFoundError as exc:
        raise typer.BadParameter(f"suite '{name}' is not implemented ({exc})") from exc
    runner: Callable[..., SuiteResult] | None = getattr(module, "run_suite", None)
    if runner is None:
        raise typer.BadParameter(f"suite '{name}' has no run_suite()")
    return runner


def _run(name: str, repo: Path, reports_dir: Path, **kwargs: Any) -> None:
    result = _load_runner(name)(repo, **kwargs)
    path = write_report(result, reports_dir)
    print_result(result)
    typer.echo(f"report: {path}")
    if not result.passed:
        raise typer.Exit(code=1)


@app.command()
def events(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """Event library: schema, sourced ranges, negative control present."""
    _run("events", repo, reports_dir)


@app.command()
def aoi(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """AOIs: geometry, grid, sources on every feature."""
    _run("aoi", repo, reports_dir)


@app.command()
def ingest(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """Manifest integrity, checksums, no NISAR BETA/PROVISIONAL mixing."""
    _run("ingest", repo, reports_dir)


@app.command()
def cube(
    repo: Path = REPO_OPTION,
    reports_dir: Path = REPORTS_OPTION,
    path: Path | None = CUBE_PATH_OPTION,
) -> None:
    """Grid/CRS consistency, time monotonic, provenance attrs."""
    _run("cube", repo, reports_dir, cube_path=path)


@app.command()
def stream(
    repo: Path = REPO_OPTION,
    reports_dir: Path = REPORTS_OPTION,
    report_dir: Path = REPLAY_DIR_OPTION,
) -> None:
    """Replay end-to-end on fixtures; CAP validates against the CAP 1.2 XSD."""
    _run("stream", repo, reports_dir, report_dir=report_dir)


@app.command()
def contracts(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """contracts/*.v0.json match the models and are valid Draft 2020-12."""
    _run("contracts", repo, reports_dir)


@app.command()
def stamp(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """Record what validate-serac proved (all required suites, git sha, tree state)."""
    result = make_stamp(repo, reports_dir)
    path = write_stamp(result, reports_dir)
    for name in REQUIRED_SUITES:
        typer.echo(f"  {name:10s} {result.suites.get(name, 'MISSING')}")
    typer.echo(
        f"stamp: {path} sha={result.git_sha} tree_clean={result.tree_clean} passed={result.passed}"
    )
    if not result.passed:
        raise typer.Exit(code=1)


def promote_command(
    repo: Path = REPO_OPTION,
    reports_dir: Path = REPORTS_OPTION,
    promotions_dir: Path = PROMOTIONS_OPTION,
) -> None:
    """Refuse unless validate-serac passed at HEAD on a clean tree; write a promotion record."""
    try:
        record = promote(repo, reports_dir, promotions_dir)
    except PromotionRefusedError as exc:
        for blocker in exc.blockers:
            typer.echo(f"refused: {blocker}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"promotable: {record.git_sha}")
