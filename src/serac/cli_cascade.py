"""`serac cascade`: the avoided-loss computation and the end-to-end replays.

Three commands:

* `serac cascade e2e --event <id>` -- run the chain and write `reports/e2e/<id>.{md,json}`.
* `serac cascade avoided-loss --request <file>` -- evaluate an `AvoidedLossRequest` supplied
  by a downstream consumer and write the response JSON. This is the entry point an
  underwriter with their own exposure schedule and replacement values would use.
* `serac cascade underwriting-table` -- the Lhende AOI table on the best available input,
  printed with the provenance header. `serac underwriting-check` calls the same code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from serac.cascade.compute import compute_avoided_loss
from serac.cascade.damage import ReplacementValueRule
from serac.cascade.exposure import load_exposure
from serac.cascade.table import print_loss_table
from serac.cascade.underwriting import UNDERWRITING_AOI, UNDERWRITING_EVENT, underwriting_table
from serac.domain.avoided_loss import AvoidedLossRequest
from serac.errors import SeracError
from serac.pipelines.e2e import EVENTS, run_e2e

app = typer.Typer(help="M5: avoided-loss computation and the end-to-end replays.")

REPO_OPTION = typer.Option(Path(), "--repo", help="Repository root.")


@app.command("e2e")
def e2e(
    event: Annotated[
        str, typer.Option("--event", help=f"One of: {', '.join(sorted(EVENTS))}")
    ] = "langtang-lhende-2026",
    repo: Path = REPO_OPTION,
    reports_dir: Annotated[Path | None, typer.Option("--reports-dir")] = None,
    execute_lfh: Annotated[
        bool,
        typer.Option("--execute-lfh/--no-execute-lfh", help="Re-run M2 offline where possible."),
    ] = True,
) -> None:
    """Run waveform -> detection -> LFH -> surrogate -> CAP -> avoided loss for one event."""
    try:
        result = run_e2e(repo, event, reports_dir=reports_dir, write=True, execute_lfh=execute_lfh)
    except SeracError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    typer.echo(f"{result.event.name}")
    for index, stage in enumerate(result.stages, start=1):
        colour = {
            "produced": typer.colors.GREEN,
            "refused": typer.colors.YELLOW,
            "did_not_fire": typer.colors.YELLOW,
            "insufficient_input": typer.colors.YELLOW,
            "not_reached": typer.colors.BRIGHT_BLACK,
            "unavailable": typer.colors.RED,
        }.get(stage.outcome.value, typer.colors.WHITE)
        typer.secho(
            f"  {index}. {stage.stage:<14} {stage.outcome.value:<18} "
            f"({stage.execution.value}) {stage.summary[:90]}",
            fg=colour,
        )
    typer.echo("")
    typer.secho(
        f"The chain stops at '{result.stopped_at}'. No cascade forecast and no CAP alert exist "
        f"for {result.event.event_id}.",
        fg=typer.colors.YELLOW,
    )
    out = (reports_dir or (repo / "reports" / "e2e")) / f"{event}.md"
    typer.echo(f"report -> {out}")


@app.command("avoided-loss")
def avoided_loss(
    request_path: Annotated[Path, typer.Option("--request", help="An AvoidedLossRequest JSON.")],
    out: Annotated[Path | None, typer.Option("--out", help="Where to write the response.")] = None,
    repo: Path = REPO_OPTION,
    aoi: Annotated[
        str | None,
        typer.Option("--aoi", help="Read installed capacities from this AOI's exposure layer."),
    ] = None,
) -> None:
    """Evaluate a caller-supplied request against the committed contract."""
    request = AvoidedLossRequest.model_validate(
        json.loads(request_path.read_text(encoding="utf-8"))
    )
    capacities = load_exposure(repo, aoi).capacities if aoi else {}
    result = compute_avoided_loss(request, capacities=capacities, rule=ReplacementValueRule())
    payload = result.response.model_dump(mode="json")
    payload["by_asset"] = [a.model_dump(mode="json") for a in result.by_asset]
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"{result.response.status} -> {out}")
    else:
        typer.echo(text)
    typer.echo(
        f"costed {len(result.determined_asset_ids)} of {len(request.exposure)} asset(s); "
        f"{len(result.undetermined)} undetermined (never zero)"
    )
    typer.echo(
        "The 'by_asset' key is NOT part of contract 0.0.0; it is a sidecar. See "
        "serac.cascade.compute for the contract change that would carry it."
    )


@app.command("underwriting-table")
def underwriting_table_command(
    repo: Path = REPO_OPTION,
    event: Annotated[str, typer.Option("--event")] = UNDERWRITING_EVENT,
    aoi: Annotated[str, typer.Option("--aoi")] = UNDERWRITING_AOI,
    out: Annotated[
        Path | None, typer.Option("--out", help="Also write the response JSON here.")
    ] = None,
) -> None:
    """Print the avoided-loss table for one AOI on the best available input."""
    table = underwriting_table(repo, aoi_id=aoi, event_id=event)
    for line in print_loss_table(table.result, table.exposure):
        typer.echo(line)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = table.result.response.model_dump(mode="json")
        payload["by_asset"] = [a.model_dump(mode="json") for a in table.result.by_asset]
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        typer.echo(f"\nresponse -> {out}")
