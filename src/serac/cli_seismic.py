"""`serac ingest fdsn|comcat|hydro`: seismic and hydrometric ingestion with `--dry-run`.

Exposes `app`, a Typer whose commands are named `fdsn`, `comcat` and `hydro` so the
orchestrator can `add_typer` it under `ingest` next to the EO adapters' commands.
`--dry-run` prints the plan and writes nothing, not even a ledger line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.errors import DatasetNotFetchedError

app = typer.Typer(name="ingest-seismic", help="FDSN waveforms, USGS ComCat, hydrometric fixture.")

RepoOption = Annotated[Path, typer.Option("--repo", help="Repository root.")]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Print the plan; write nothing.")]


@app.callback()
def _group() -> None:
    """Seismic and hydrometric ingestion."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@app.command("fdsn")
def fdsn(
    event: Annotated[str, typer.Option("--event", help="Event id for the destination and ledger.")],
    start: Annotated[datetime, typer.Option("--start", help="Window start (UTC).")],
    end: Annotated[datetime, typer.Option("--end", help="Window end (UTC).")],
    sncl: Annotated[
        list[str] | None,
        typer.Option("--sncl", help="NET.STA.LOC.CHA (repeatable); else search by radius."),
    ] = None,
    lat: Annotated[float | None, typer.Option("--lat")] = None,
    lon: Annotated[float | None, typer.Option("--lon")] = None,
    radius_km: Annotated[float, typer.Option("--radius-km")] = 300.0,
    max_stations: Annotated[int, typer.Option("--max-stations")] = 4,
    client: Annotated[str, typer.Option("--client", help="ObsPy FDSN alias.")] = "EARTHSCOPE",
    dry_run: DryRun = False,
    repo: RepoOption = Path("."),
) -> None:
    """Plan or fetch archived waveforms (+ StationXML) into data/raw/fdsn_waveforms/<event>/."""
    from serac.adapters.seismic.fdsn import FdsnWaveformArchive
    from serac.domain.seismic import Sncl
    from serac.ports.seismic import StationQuery, WaveformRequest

    archive = FdsnWaveformArchive(client_name=client, repo_root=repo)
    start_utc, end_utc = _aware(start), _aware(end)
    if sncl:
        sncls = [Sncl.from_key(key) for key in sncl]
    else:
        if lat is None or lon is None:
            raise typer.BadParameter("give --sncl or both --lat and --lon")
        found = archive.search_stations(
            StationQuery(
                latitude=lat,
                longitude=lon,
                max_radius_km=radius_km,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        )
        for ref in found[:max_stations]:
            typer.echo(f"station {ref.sncl.key} {ref.distance_km:.0f} km {ref.data_centre}")
        sncls = [ref.sncl for ref in found[:max_stations]]
        if not sncls:
            typer.echo("no channels found", err=True)
            raise typer.Exit(code=1)
    request = WaveformRequest(event_id=event, sncls=sncls, start_utc=start_utc, end_utc=end_utc)
    plan = archive.plan(request)
    typer.echo(f"data centre: {plan.data_centre}")
    for row in plan.bulk:
        typer.echo("bulk: " + " ".join(row))
    typer.echo(f"estimated bytes: {plan.estimated_bytes} ({plan.estimate_basis})")
    for warning in plan.warnings:
        typer.echo(f"warning: {warning}")
    if dry_run:
        typer.echo("dry run: nothing written")
        return
    dest = repo / "data" / "raw" / "fdsn_waveforms" / event
    result = archive.fetch(plan, dest, JsonlManifestLedger(repo / "data" / "manifest.jsonl"))
    typer.echo(f"status: {result.status}; files: {', '.join(result.files)}")
    if result.missing:
        typer.echo(f"missing: {', '.join(result.missing)}")


@app.command("comcat")
def comcat(
    start: Annotated[datetime, typer.Option("--start", help="UTC.")],
    end: Annotated[datetime, typer.Option("--end", help="UTC.")],
    event_type: Annotated[str | None, typer.Option("--event-type")] = "landslide",
    event_id: Annotated[str | None, typer.Option("--event-id")] = None,
    min_magnitude: Annotated[float | None, typer.Option("--min-magnitude")] = None,
    dry_run: DryRun = False,
    repo: RepoOption = Path("."),
) -> None:
    """Plan or fetch a ComCat event query into data/raw/usgs_comcat/ (US public domain)."""
    from serac.adapters.seismic.usgs_comcat import ComCatCatalog
    from serac.ports.seismic import CatalogQuery

    query = CatalogQuery(
        start_utc=_aware(start),
        end_utc=_aware(end),
        event_type=event_type,
        event_id=event_id,
        min_magnitude=min_magnitude,
    )
    catalog = ComCatCatalog(repo_root=repo)
    for key, value in catalog.plan(query).as_dict().items():
        typer.echo(f"{key}: {value}")
    if dry_run:
        typer.echo("dry run: nothing written")
        return
    written = catalog.fetch(
        query, repo / "data" / "raw", JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    )
    typer.echo("wrote: " + ", ".join(str(p) for p in written))


@app.command("hydro")
def hydro(
    station: Annotated[
        str | None, typer.Option("--station", help="Station id, e.g. galchhi.")
    ] = None,
    dry_run: DryRun = False,
    repo: RepoOption = Path("."),
) -> None:
    """Show the ICIMOD-reported hydrometric fixture; nothing is fetched (no open feed exists)."""
    from serac.adapters.hydro.icimod_fixture import DEFAULT_FIXTURE, IcimodReportedHydrometric

    path = repo / DEFAULT_FIXTURE
    source = IcimodReportedHydrometric(path)
    typer.echo(f"fixture: {path} status={source.fixture.status}")
    typer.echo("no open real-time Nepal/China hydrometric feed exists; values are reported figures")
    if dry_run:
        return
    try:
        stations = source.stations()
    except DatasetNotFetchedError as exc:
        typer.echo(f"not fetched: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    day = source.fixture.event_date
    for st in stations:
        if station and st.station_id != station:
            continue
        typer.echo(f"station {st.station_id}: {st.name}")
        if day is None:
            continue
        window = (
            datetime(day.year, day.month, day.day, tzinfo=UTC),
            datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC),
        )
        try:
            for obs in source.observations(st.station_id, window):
                typer.echo(
                    f"  {obs.variable}={obs.value} over {obs.interval_s}s "
                    f"time={obs.time_utc} ({obs.time_basis[:40]}...) source={obs.source_ref}"
                )
        except DatasetNotFetchedError as exc:
            typer.echo(f"  {exc}")


if __name__ == "__main__":  # pragma: no cover
    app()
