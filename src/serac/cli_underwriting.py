"""`serac underwriting-check`: contract round-trip, then the real avoided-loss table.

In Prompt 1 this command exited 2 with "not implemented: Prompt 2". It now **computes**: the
contract round-trip still runs first (it is the cheapest way to catch a schema drift), and
then the avoided-loss computation runs for the Lhende AOI on the best input serac actually
has, printing the per-asset table under a provenance header.

Exit codes:

* `0` -- the round-trip passed and the computation ran. It ran; it did not necessarily cost
  anything. Read the header.
* `1` -- the contract round-trip failed, or the computation could not be built at all.

`--no-table` restores the Prompt 1 behaviour of the round-trip alone (still exit 0).

Exposes `app` (a Typer whose bare invocation runs the check) and `underwriting_check` (the
command function) so `serac.cli` can mount either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from serac.cascade.table import print_loss_table
from serac.cascade.underwriting import UNDERWRITING_AOI, UNDERWRITING_EVENT, underwriting_table
from serac.errors import SeracError
from serac.validation.underwriting import run_underwriting_check

ContractsOption = Annotated[
    Path, typer.Option("--contracts", help="Directory holding <name>.v0.json files.")
]
RepoOption = Annotated[Path, typer.Option("--repo", help="Repository root.")]
TableOption = Annotated[
    bool, typer.Option("--table/--no-table", help="Run and print the avoided-loss table.")
]
AoiOption = Annotated[str, typer.Option("--aoi", help="AOI to cost.")]
EventOption = Annotated[str, typer.Option("--event", help="Replay whose inputs are used.")]

app = typer.Typer(
    name="underwriting-check",
    help="Round-trip the AvoidedLoss contracts, then compute and print the avoided-loss table.",
    invoke_without_command=True,
)


def underwriting_check(
    contracts: ContractsOption = Path("contracts"),
    repo: RepoOption = Path(),
    table: TableOption = True,
    aoi: AoiOption = UNDERWRITING_AOI,
    event: EventOption = UNDERWRITING_EVENT,
) -> None:
    """Validate the AvoidedLoss contracts, then compute the avoided-loss table."""
    result = run_underwriting_check(contracts)
    for step in result.passed:
        typer.echo(f"ok: {step}")
    for failure in result.failures:
        typer.echo(f"FAIL: {failure}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)
    if not table:
        return

    typer.echo("")
    try:
        built = underwriting_table(repo, aoi_id=aoi, event_id=event)
    except (SeracError, OSError, ValueError) as exc:
        typer.secho(f"FAIL: could not build the table: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for line in print_loss_table(built.result, built.exposure):
        typer.echo(line)
    typer.echo("")
    typer.secho(
        f"COMPUTED: {built.costed} of {built.total} exposed asset(s) in {built.aoi_id} could be "
        f"costed on the best available input for {built.event_id}. The remaining "
        f"{built.total - built.costed} are UNDETERMINED, which is not the same as zero loss.",
        fg=typer.colors.YELLOW if built.costed < built.total else typer.colors.GREEN,
    )


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    contracts: ContractsOption = Path("contracts"),
    repo: RepoOption = Path(),
    table: TableOption = True,
    aoi: AoiOption = UNDERWRITING_AOI,
    event: EventOption = UNDERWRITING_EVENT,
) -> None:
    """Validate the AvoidedLoss contracts, then compute the avoided-loss table."""
    if ctx.invoked_subcommand is None:
        underwriting_check(contracts, repo, table, aoi, event)


if __name__ == "__main__":  # pragma: no cover
    app()
