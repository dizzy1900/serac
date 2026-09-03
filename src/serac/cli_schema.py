"""`serac schema ...`: export and check the JSON-Schema contracts.

Exposes `app`, which `serac.cli` mounts as the `schema` sub-command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from serac.domain.schema_export import check_contracts, discover_contracts, write_contracts

app = typer.Typer(name="schema", help="JSON-Schema contracts in contracts/.", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """JSON-Schema contracts in contracts/."""


@app.command("export")
def export(
    out: Annotated[Path, typer.Option("--out", help="Directory of <name>.v0.json files.")] = Path(
        "contracts"
    ),
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Do not write; exit 1 listing contracts that drift from disk."
        ),
    ] = False,
) -> None:
    """Write every registered contract as contracts/<name>.v0.json (or --check for drift)."""
    if check:
        drift = check_contracts(out)
        if drift:
            typer.echo(f"contract drift in {out}: " + ", ".join(drift), err=True)
            typer.echo("run `serac schema export` and commit the result", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"{len(discover_contracts())} contracts up to date in {out}")
        return
    for path in write_contracts(out):
        typer.echo(str(path))


if __name__ == "__main__":  # pragma: no cover
    app()
