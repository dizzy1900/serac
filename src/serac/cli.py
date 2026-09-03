"""`serac` command-line entry point.

Sub-commands are registered by the modules that own them; this file only assembles the app.
"""

from __future__ import annotations

import typer

from serac import __version__

app = typer.Typer(
    name="serac",
    help="serac: open model of high-mountain rock-ice avalanche cascades.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"serac {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Print version."
    ),
) -> None:
    """serac command-line interface."""


if __name__ == "__main__":  # pragma: no cover
    app()
