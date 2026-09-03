"""`serac` command-line entry point.

Sub-commands are registered by the modules that own them; this file only assembles the app.
"""

from __future__ import annotations

import typer

from serac import (
    __version__,
    cli_aoi,
    cli_cube,
    cli_data,
    cli_events,
    cli_ingest,
    cli_lfh,
    cli_models,
    cli_schema,
    cli_seismic,
    cli_sources,
    cli_stream,
    cli_underwriting,
    cli_validate,
)

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
app.add_typer(
    cli_events.app, name="events", help="Event library: add, validate, build-index, report."
)
app.add_typer(cli_cube.app, name="cube", help="Build and inspect per-AOI feature cubes.")
app.add_typer(cli_sources.app, name="sources", help="Fetch, hash and ledger source documents.")
app.add_typer(cli_aoi.app, name="aoi", help="AOI library: build, validate, describe.")
app.add_typer(cli_data.app, name="data", help="Assemble model training sets.")
app.add_typer(cli_models.app, name="models", help="Train and evaluate model components.")
app.add_typer(cli_lfh.app, name="lfh", help="Landslide force-history inversion (M2).")
app.add_typer(cli_validate.app, name="validate")
app.command("promote")(cli_validate.promote_command)
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
