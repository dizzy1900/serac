"""`serac underwriting-check`: avoided-loss schema round-trip, then exit 2 "not implemented".

Exposes `app` (a Typer whose bare invocation runs the check) and `underwriting_check` (the
command function) so `serac.cli` can mount either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from serac.validation.underwriting import (
    NOT_IMPLEMENTED_EXIT_CODE,
    NOT_IMPLEMENTED_MESSAGE,
    run_underwriting_check,
)

ContractsOption = Annotated[
    Path, typer.Option("--contracts", help="Directory holding <name>.v0.json files.")
]

app = typer.Typer(
    name="underwriting-check",
    help="Round-trip the AvoidedLoss contracts; exits 2 because the computation is Prompt 2.",
    invoke_without_command=True,
)


def underwriting_check(
    contracts: ContractsOption = Path("contracts"),
) -> None:
    """Validate the AvoidedLossRequest/Response examples against the committed contracts."""
    result = run_underwriting_check(contracts)
    for step in result.passed:
        typer.echo(f"ok: {step}")
    for failure in result.failures:
        typer.echo(f"FAIL: {failure}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)
    typer.echo(NOT_IMPLEMENTED_MESSAGE, err=True)
    raise typer.Exit(code=NOT_IMPLEMENTED_EXIT_CODE)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    contracts: ContractsOption = Path("contracts"),
) -> None:
    """Validate the AvoidedLossRequest/Response examples against the committed contracts."""
    if ctx.invoked_subcommand is None:
        underwriting_check(contracts)


if __name__ == "__main__":  # pragma: no cover
    app()
