"""`serac sources ...`: fetch, hash and ledger the documents an event record cites.

Exposes `app`, which `serac.cli` mounts as the `sources` sub-command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.common import SourceKind
from serac.pipelines.sources import (
    DEFAULT_TIMEOUT_S,
    FetchRequest,
    HttpxClient,
    SourceFetchError,
    fetch_source,
)

app = typer.Typer(
    name="sources",
    help="Retrieve, hash and ledger the source documents cited by event records.",
    no_args_is_help=True,
)


@app.callback()
def _group() -> None:
    """Source documents for the event library."""


def _split(csv: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in csv.split(",") if part.strip())


@app.command("fetch")
def fetch(
    url: Annotated[str, typer.Argument(help="Document URL (landing page, PDF, API response).")],
    event: Annotated[str, typer.Option("--event", help="Event record id the source belongs to.")],
    id: Annotated[str, typer.Option("--id", help="Slug used as the SourceRef id.")],
    kind: Annotated[SourceKind, typer.Option("--kind", help="SourceRef kind.")],
    licence: Annotated[str, typer.Option("--licence", help="SPDX id or licence as stated.")],
    claims: Annotated[
        str, typer.Option("--claims", help="Comma-separated record field paths this source backs.")
    ],
    doi: Annotated[
        str | None, typer.Option("--doi", help="DOI to resolve via Crossref before citing.")
    ] = None,
    store: Annotated[
        bool, typer.Option("--store", help="Keep a copy under data/raw/sources/<event>/.")
    ] = False,
    apply: Annotated[
        bool, typer.Option("--apply", help="Insert the SourceRef into data/events/<event>.json.")
    ] = False,
    title: Annotated[
        str | None, typer.Option("--title", help="Override the extracted title.")
    ] = None,
    authors: Annotated[
        str | None, typer.Option("--authors", help="Comma-separated authors (overrides Crossref).")
    ] = None,
    year: Annotated[int | None, typer.Option("--year", help="Publication year.")] = None,
    publisher: Annotated[str | None, typer.Option("--publisher", help="Publisher.")] = None,
    excerpt: Annotated[
        str | None,
        typer.Option("--excerpt", help="Short quote (<=300 chars) supporting the claims."),
    ] = None,
    licence_source_url: Annotated[
        str | None, typer.Option("--licence-source-url", help="Where the licence is stated.")
    ] = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Ledger notes.")] = None,
    repo: Annotated[Path, typer.Option("--repo", help="Repository root.")] = Path("."),
    ledger: Annotated[
        Path | None,
        typer.Option("--ledger", help="Ledger path (default <repo>/data/manifest.jsonl)."),
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds.")] = DEFAULT_TIMEOUT_S,
) -> None:
    """GET a document, hash it, resolve its DOI, ledger it and print the SourceRef JSON."""
    request = FetchRequest(
        url=url,
        event_id=event,
        source_id=id,
        kind=kind,
        licence=licence,
        claims=_split(claims),
        doi=doi,
        store=store,
        title=title,
        authors=_split(authors) if authors else (),
        year=year,
        publisher=publisher,
        excerpt=excerpt,
        licence_source_url=licence_source_url,
        notes=notes,
        timeout_s=timeout,
    )
    ledger_path = ledger or repo / "data" / "manifest.jsonl"
    try:
        outcome = fetch_source(
            HttpxClient(), request, JsonlManifestLedger(ledger_path), repo.resolve(), apply=apply
        )
    except SourceFetchError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"fetched {outcome.response.final_url} ({outcome.response.content_type}, "
        f"{len(outcome.response.content)} bytes) sha256={outcome.source.sha256[:12]}… "
        f"status={outcome.entry.status.value}",
        err=True,
    )
    if outcome.crossref is not None:
        typer.echo(f"doi resolved via Crossref: {outcome.crossref.doi}", err=True)
    if outcome.stored_path is not None:
        typer.echo(f"stored copy: {outcome.source.stored_copy}", err=True)
    for warning in outcome.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if outcome.applied_to is not None:
        typer.echo(f"applied to {outcome.applied_to}", err=True)
    typer.echo(json.dumps(json.loads(outcome.source.model_dump_json()), indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    app()
