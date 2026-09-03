"""`serac` command-line entry point.

Sub-commands are registered by the modules that own them; this file only assembles the app.
"""

from __future__ import annotations

import typer

from serac import __version__, cli_ingest, cli_schema, cli_seismic, cli_stream, cli_underwriting

app = typer.Typer(
    name="serac",
    help="serac: open model of high-mountain rock-ice avalanche cascades.",
    no_args_is_help=True,
)
app.add_typer(cli_ingest.app, name="ingest", help="Fetch products into data/raw with provenance.")
app.add_typer(cli_stream.app, name="stream", help="Real-time lane: run stages, replay, golden.")
app.command("replay")(cli_stream.replay)
for _name in ("fdsn", "comcat", "hydro"):
    cli_ingest.app.command(_name)(getattr(cli_seismic, _name))
app.add_typer(cli_schema.app, name="schema", help="Export/check the JSON-Schema contracts.")
app.command("underwriting-check")(cli_underwriting.underwriting_check)


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
