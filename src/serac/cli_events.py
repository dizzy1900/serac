"""`serac events ...`: add, validate, index and report on the event library.

Exposes `app`, which `serac.cli` mounts as the `events` sub-command.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.events import MassMovementEvent
from serac.pipelines.coverage import build_report, render_json, render_markdown, render_table
from serac.pipelines.event_entry import interactive_record
from serac.pipelines.events_index import build_index
from serac.pipelines.sources import dump_record
from serac.validation.events import run_suite
from serac.validation.result import print_result, write_report

app = typer.Typer(
    name="events",
    help="The event library: add records, validate, rebuild the index, report coverage.",
    no_args_is_help=True,
)

RepoOption = Annotated[Path, typer.Option("--repo", help="Repository root.")]
EventsDirOption = Annotated[
    Path | None,
    typer.Option("--events-dir", help="Records directory (default <repo>/data/events)."),
]
LedgerOption = Annotated[
    Path | None, typer.Option("--ledger", help="Ledger path (default <repo>/data/manifest.jsonl).")
]


class ReportFormat(StrEnum):
    table = "table"
    json = "json"
    markdown = "markdown"


@app.callback()
def _group() -> None:
    """The event library in data/events/."""


def _events_dir(repo: Path, events_dir: Path | None) -> Path:
    return events_dir if events_dir is not None else repo / "data" / "events"


def _ledger_path(repo: Path, ledger: Path | None) -> Path:
    return ledger if ledger is not None else repo / "data" / "manifest.jsonl"


def _print_validation_error(exc: ValidationError) -> None:
    typer.echo("record rejected; nothing written:", err=True)
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        typer.echo(f"  {loc}: {err['msg']}", err=True)


def _ask(question: str) -> str:
    answer: str = typer.prompt(question, default="", show_default=False)
    return answer


@app.command("add")
def add(
    from_json: Annotated[
        Path | None,
        typer.Option(
            "--from-json",
            help="Load the record from a JSON file instead of prompting (the only way to "
            "enter infrastructure_impacts, precursors_observed, transect_observations and "
            "related_seismic).",
        ),
    ] = None,
    repo: RepoOption = Path("."),
    events_dir: EventsDirOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing record with the same id.")
    ] = False,
) -> None:
    """Validate a record and write data/events/<event_id>.json in canonical form."""
    data: Any
    if from_json is not None:
        try:
            data = json.loads(from_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            typer.echo(f"{from_json}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    else:
        data = interactive_record(_ask)
    try:
        event = MassMovementEvent.model_validate(data)
    except ValidationError as exc:
        _print_validation_error(exc)
        raise typer.Exit(code=1) from exc
    out = _events_dir(repo, events_dir) / f"{event.event_id}.json"
    if out.exists() and not force:
        typer.echo(f"{out} exists; pass --force to overwrite", err=True)
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_record(event.model_dump(mode="json")), encoding="utf-8")
    typer.echo(str(out))


@app.command("validate")
def validate(repo: RepoOption = Path(".")) -> None:
    """Run the `events` validation suite and write reports/validation/events.json."""
    result = run_suite(repo)
    print_result(result)
    path = write_report(result, repo / "reports" / "validation")
    typer.echo(f"report: {path}", err=True)
    if not result.passed:
        raise typer.Exit(code=1)


@app.command("build-index")
def build_index_command(
    repo: RepoOption = Path("."),
    events_dir: EventsDirOption = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Parquet path (default <events-dir>/events.parquet)."),
    ] = None,
) -> None:
    """Rebuild the GeoParquet index data/events/events.parquet from the records."""
    try:
        path = build_index(_events_dir(repo, events_dir), out)
    except ValidationError as exc:
        _print_validation_error(exc)
        raise typer.Exit(code=1) from exc
    typer.echo(str(path))


@app.command("report")
def report(
    fmt: Annotated[
        ReportFormat, typer.Option("--format", help="Output format.")
    ] = ReportFormat.table,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the report here instead of stdout.")
    ] = None,
    repo: RepoOption = Path("."),
    events_dir: EventsDirOption = None,
    ledger: LedgerOption = None,
) -> None:
    """Coverage matrix (records x data products x pre/event/post) from the provenance ledger.

    Exits 1 when any source reference is unresolved or any `best` lacks a qualifying source.
    """
    try:
        coverage = build_report(
            _events_dir(repo, events_dir), JsonlManifestLedger(_ledger_path(repo, ledger))
        )
    except ValidationError as exc:
        _print_validation_error(exc)
        raise typer.Exit(code=1) from exc
    renderers = {
        ReportFormat.table: render_table,
        ReportFormat.json: render_json,
        ReportFormat.markdown: render_markdown,
    }
    text = renderers[fmt](coverage)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)
    else:
        typer.echo(text, nl=False)
    typer.echo(coverage.footer(), err=True)
    if not coverage.ok:
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
