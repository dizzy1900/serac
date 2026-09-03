"""`serac ingest ...` commands for the EO adapters.

Wire into `serac.cli` with `app.add_typer(cli_ingest.app, name="ingest")`.

Every command takes exactly one of `--dry-run` (print the plan, write nothing, not even a
ledger line) or `--yes` (execute the plan; the > 5 GB gate still asks interactively).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from serac.adapters.eo.dem_glo30 import DEFAULT_BUFFER_M, Glo30DemAdapter
from serac.adapters.eo.earthsearch_sentinel2 import (
    DEFAULT_MAX_CLOUD_PERCENT,
    EarthSearchSentinel2Adapter,
    PystacSearchClient,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import ManifestEntry
from serac.errors import CredentialsMissingError, FetchDeclinedError, IngestRefusedError
from serac.ports.ingest import Bbox4326, DryRunPlan, IngestAdapter, IngestRequest
from serac.settings import get_settings

app = typer.Typer(
    name="ingest",
    help="Fetch external products into data/raw/ with a provenance entry per file.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

EXIT_USAGE = 2
EXIT_CREDENTIALS = 3
EXIT_DECLINED = 4
EXIT_REFUSED = 5
EXIT_FETCH_FAILED = 6


def parse_bbox(text: str) -> Bbox4326:
    """`W,S,E,N` in degrees."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise typer.BadParameter("bbox must be W,S,E,N in degrees")
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"bbox values must be numbers: {exc}") from exc
    return (w, s, e, n)


def load_aoi_bbox(aoi_id: str, data_dir: Path) -> Bbox4326 | None:
    """`bbox_4326` from `data/aoi/<id>/aoi.json` when the AOI has been built, else None."""
    path = data_dir / "aoi" / aoi_id / "aoi.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw = doc.get("bbox_4326") or doc.get("bbox")
    if raw is None or len(raw) != 4:
        return None
    w, s, e, n = (float(v) for v in raw)
    return (w, s, e, n)


def resolve_bbox(aoi_id: str, bbox: str | None, data_dir: Path) -> Bbox4326:
    if bbox is not None:
        return parse_bbox(bbox)
    found = load_aoi_bbox(aoi_id, data_dir)
    if found is None:
        err_console.print(
            f"[red]no bbox:[/red] pass --bbox W,S,E,N or build data/aoi/{aoi_id}/aoi.json first"
        )
        raise typer.Exit(EXIT_USAGE)
    return found


def parse_date(text: str, *, end: bool = False) -> datetime:
    """ISO date or datetime; a bare date means 00:00Z (or 23:59:59Z for `end`)."""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"not an ISO date: {text}") from exc
    if len(text) == 10 and end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _human(n: int | None) -> str:
    if n is None:
        return "unknown"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def print_plan(plan: DryRunPlan) -> None:
    table = Table(title=f"{plan.adapter} v{plan.adapter_version} dry run: {plan.source.value}")
    table.add_column("product", overflow="fold")
    table.add_column("level")
    table.add_column("time")
    table.add_column("est. bytes", justify="right")
    table.add_column("url / assets", overflow="fold")
    for p in plan.products:
        when = p.time_start.date().isoformat() if p.time_start else "-"
        where = p.url or ", ".join(sorted(p.assets)) or "-"
        table.add_row(p.product_id, p.product_level or "-", when, _human(p.estimated_bytes), where)
    console.print(table)
    for p in plan.products:
        tiles = p.properties.get("tiles")
        detail = ", ".join(tiles) if tiles else (p.url or ", ".join(sorted(p.assets)) or "-")
        console.print(f"  {p.product_id}: {detail}", soft_wrap=True, highlight=False)
    console.print(f"AOI [bold]{plan.request.aoi_id}[/bold] bbox {plan.request.bbox_4326}")
    console.print(f"Total estimate: [bold]{_human(plan.estimated_bytes)}[/bold]")
    console.print(f"Estimate basis: {plan.estimate_basis}")
    for spec in plan.requires_credentials:
        console.print(
            f"[yellow]needs credential:[/yellow] {spec.name} ({', '.join(spec.env_vars)})"
        )
    for w in plan.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")
    for r in plan.refusals:
        console.print(f"[red]refusal:[/red] {r}")


def print_entries(entries: list[ManifestEntry]) -> None:
    table = Table(title="ledger entries appended")
    table.add_column("status")
    table.add_column("product")
    table.add_column("path")
    table.add_column("bytes", justify="right")
    table.add_column("sha256")
    for e in entries:
        table.add_row(
            e.status.value,
            e.product_id,
            e.path or "-",
            _human(e.size_bytes),
            (e.sha256 or "-")[:12],
        )
    console.print(table)


def run(
    adapter: IngestAdapter, request: IngestRequest, *, dry_run: bool, yes: bool, data_dir: Path
) -> None:
    if dry_run == yes:
        err_console.print("[red]pass exactly one of --dry-run or --yes[/red]")
        raise typer.Exit(EXIT_USAGE)
    plan = adapter.plan(request)
    print_plan(plan)
    if dry_run:
        console.print("[dim]dry run: nothing written, no ledger entry.[/dim]")
        return
    ledger = JsonlManifestLedger(data_dir / "manifest.jsonl")
    try:
        entries = adapter.fetch(
            plan,
            dest_root=data_dir,
            ledger=ledger,
            confirm=lambda question: typer.confirm(question, default=False),
        )
    except CredentialsMissingError as exc:
        err_console.print(f"[red]credentials missing:[/red] {exc}")
        raise typer.Exit(EXIT_CREDENTIALS) from exc
    except FetchDeclinedError as exc:
        err_console.print(f"[yellow]declined:[/yellow] {exc}")
        raise typer.Exit(EXIT_DECLINED) from exc
    except IngestRefusedError as exc:
        err_console.print(f"[red]refused:[/red] {exc}")
        raise typer.Exit(EXIT_REFUSED) from exc
    except Exception as exc:
        err_console.print(f"[red]fetch failed (recorded as failed in the ledger):[/red] {exc}")
        raise typer.Exit(EXIT_FETCH_FAILED) from exc
    print_entries(entries)


AoiOpt = Annotated[str, typer.Option("--aoi", help="AOI id, e.g. chamoli-rishiganga")]
BboxOpt = Annotated[
    str | None, typer.Option("--bbox", help="W,S,E,N degrees; defaults to data/aoi/<id>/aoi.json")
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Print the plan; write nothing.")]
YesOpt = Annotated[bool, typer.Option("--yes", help="Execute the plan.")]
DataDirOpt = Annotated[
    Path | None, typer.Option("--data-dir", help="Defaults to SERAC_DATA_DIR (data/).")
]


@app.command("dem")
def dem(
    aoi: AoiOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    buffer_m: Annotated[
        float, typer.Option(help="Buffer around the bbox, metres.")
    ] = DEFAULT_BUFFER_M,
    full_tiles: Annotated[
        bool,
        typer.Option("--full-tiles", help="Download whole 1x1 degree tiles instead of a window."),
    ] = False,
    data_dir: DataDirOpt = None,
) -> None:
    """Copernicus GLO-30 DEM: windowed COG crop (default) or whole tiles."""
    root = data_dir or get_settings().serac_data_dir
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        params={"buffer_m": buffer_m, "full_tiles": full_tiles},
    )
    run(Glo30DemAdapter(), request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("s2-earthsearch")
def s2_earthsearch(
    aoi: AoiOpt,
    from_: Annotated[str, typer.Option("--from", help="Start date (ISO).")],
    to: Annotated[str, typer.Option("--to", help="End date (ISO, inclusive).")],
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    max_cloud: Annotated[
        float, typer.Option("--max-cloud", help="Tile-level eo:cloud_cover ceiling, percent.")
    ] = DEFAULT_MAX_CLOUD_PERCENT,
    max_scenes: Annotated[
        int | None, typer.Option("--max-scenes", help="Keep the N least-cloudy scenes.")
    ] = None,
    stac_url: Annotated[
        str, typer.Option("--stac-url")
    ] = "https://earth-search.aws.element84.com/v1",
    data_dir: DataDirOpt = None,
) -> None:
    """Sentinel-2 L2A B03/B11/SCL windows from Earth Search (public COGs)."""
    root = data_dir or get_settings().serac_data_dir
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=parse_date(from_),
        time_end=parse_date(to, end=True),
        params={"max_cloud": max_cloud, "max_scenes": max_scenes},
    )
    adapter = EarthSearchSentinel2Adapter(PystacSearchClient(stac_url))
    run(adapter, request, dry_run=dry_run, yes=yes, data_dir=root)


# -- credentialed adapters: the real request/poll/download paths, refused without .env -----------


def _window(from_: str, to: str) -> tuple[datetime, datetime]:
    return parse_date(from_), parse_date(to, end=True)


FromOpt = Annotated[str, typer.Option("--from", help="Start date (ISO).")]
ToOpt = Annotated[str, typer.Option("--to", help="End date (ISO, inclusive).")]
RelativeOrbitOpt = Annotated[
    int | None, typer.Option("--relative-orbit", help="Keep one Sentinel-1 track (pathNumber).")
]
FlightDirectionOpt = Annotated[
    str | None, typer.Option("--flight-direction", help="ASCENDING or DESCENDING.")
]


@app.command("s1")
def s1(
    aoi: AoiOpt,
    from_: FromOpt,
    to: ToOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    level: Annotated[str, typer.Option("--level", help="SLC or GRD_HD.")] = "SLC",
    relative_orbit: RelativeOrbitOpt = None,
    flight_direction: FlightDirectionOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Sentinel-1 IW granules via ASF search; download needs Earthdata Login."""
    from serac.adapters.eo.asf_sentinel1 import Sentinel1AsfAdapter

    root = data_dir or get_settings().serac_data_dir
    t0, t1 = _window(from_, to)
    params: dict[str, object] = {"processing_level": level}
    if relative_orbit is not None:
        params["relative_orbit"] = relative_orbit
    if flight_direction is not None:
        params["flight_direction"] = flight_direction
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=t0,
        time_end=t1,
        params=params,
    )
    run(Sentinel1AsfAdapter(), request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("hyp3")
def hyp3(
    aoi: AoiOpt,
    from_: FromOpt,
    to: ToOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    poll: Annotated[
        bool, typer.Option("--poll", help="Refresh submitted jobs and download finished ones.")
    ] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Block until the jobs finish.")] = False,
    max_days: Annotated[float, typer.Option("--max-days", help="Pair temporal baseline.")] = 12.0,
    relative_orbit: RelativeOrbitOpt = None,
    flight_direction: FlightDirectionOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """HyP3 INSAR_GAMMA jobs for same-track Sentinel-1 pairs (submit / --poll / --wait)."""
    from serac.adapters.eo.hyp3_insar import Hyp3InsarAdapter

    root = data_dir or get_settings().serac_data_dir
    t0, t1 = _window(from_, to)
    params: dict[str, object] = {"max_days": max_days, "wait": wait}
    if relative_orbit is not None:
        params["relative_orbit"] = relative_orbit
    if flight_direction is not None:
        params["flight_direction"] = flight_direction
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=t0,
        time_end=t1,
        params=params,
    )
    adapter = Hyp3InsarAdapter()
    if poll:
        if dry_run:
            err_console.print("[red]--poll cannot be combined with --dry-run[/red]")
            raise typer.Exit(EXIT_USAGE)
        plan = adapter.plan(request)
        ledger = JsonlManifestLedger(root / "manifest.jsonl")
        try:
            entries = adapter.poll(plan, dest_root=root, ledger=ledger)
        except CredentialsMissingError as exc:
            err_console.print(f"[red]credentials missing:[/red] {exc}")
            raise typer.Exit(EXIT_CREDENTIALS) from exc
        print_entries(entries)
        return
    run(adapter, request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("nisar")
def nisar(
    aoi: AoiOpt,
    bbox: BboxOpt = None,
    from_: Annotated[str | None, typer.Option("--from", help="Start date (ISO).")] = None,
    to: Annotated[str | None, typer.Option("--to", help="End date (ISO, inclusive).")] = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    level: Annotated[
        str | None,
        typer.Option("--level", help="beta or provisional; required when both match."),
    ] = None,
    processing_level: Annotated[
        str, typer.Option("--processing-level", help="Science level, e.g. GCOV, GSLC, GUNW.")
    ] = "GCOV",
    flight_direction: FlightDirectionOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """NISAR science granules via ASF; refuses to mix BETA and PROVISIONAL."""
    from serac.adapters.eo.nisar import NisarAdapter

    root = data_dir or get_settings().serac_data_dir
    params: dict[str, object] = {"processing_level": processing_level}
    if level is not None:
        params["level"] = level
    if flight_direction is not None:
        params["flight_direction"] = flight_direction
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=parse_date(from_) if from_ else None,
        time_end=parse_date(to, end=True) if to else None,
        params=params,
    )
    run(NisarAdapter(), request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("s2-cdse")
def s2_cdse(
    aoi: AoiOpt,
    from_: FromOpt,
    to: ToOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    max_cloud: Annotated[
        float, typer.Option("--max-cloud", help="Tile-level eo:cloud_cover ceiling, percent.")
    ] = DEFAULT_MAX_CLOUD_PERCENT,
    max_scenes: Annotated[
        int | None, typer.Option("--max-scenes", help="Keep the N least-cloudy scenes.")
    ] = None,
    stac_url: Annotated[
        str, typer.Option("--stac-url")
    ] = "https://stac.dataspace.copernicus.eu/v1",
    data_dir: DataDirOpt = None,
) -> None:
    """Sentinel-2 L2A B03/B11/SCL windows from CDSE (OAuth client credentials)."""
    from serac.adapters.eo.cdse_sentinel2 import CdseSentinel2Adapter

    root = data_dir or get_settings().serac_data_dir
    t0, t1 = _window(from_, to)
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=t0,
        time_end=t1,
        params={"max_cloud": max_cloud, "max_scenes": max_scenes},
    )
    adapter = CdseSentinel2Adapter(PystacSearchClient(stac_url))
    run(adapter, request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("era5")
def era5(
    aoi: AoiOpt,
    from_: FromOpt,
    to: ToOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    variable: Annotated[
        list[str] | None, typer.Option("--variable", help="CDS variable name (repeatable).")
    ] = None,
    data_dir: DataDirOpt = None,
) -> None:
    """ERA5 hourly single-level fields over the AOI cells via cdsapi (needs CDSAPI_KEY)."""
    from serac.adapters.eo.era5_cds import Era5Adapter

    root = data_dir or get_settings().serac_data_dir
    t0, t1 = _window(from_, to)
    params: dict[str, object] = {}
    if variable:
        params["variables"] = list(variable)
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        time_start=t0,
        time_end=t1,
        params=params,
    )
    run(Era5Adapter(), request, dry_run=dry_run, yes=yes, data_dir=root)


@app.command("gacos")
def gacos(
    aoi: AoiOpt,
    bbox: BboxOpt = None,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
    date: Annotated[
        list[str] | None, typer.Option("--date", help="SAR acquisition date YYYYMMDD (repeatable).")
    ] = None,
    time_utc: Annotated[
        str, typer.Option("--time-utc", help="Acquisition time HH:MM UTC.")
    ] = "00:00",
    request_id: Annotated[
        str | None,
        typer.Option("--request-id", help="Your id for the request (or with --receive)."),
    ] = None,
    receive: Annotated[
        str | None, typer.Option("--receive", help="Delivery URL from the GACOS e-mail.")
    ] = None,
    poll: Annotated[bool, typer.Option("--poll", help="Report the state of --request-id.")] = False,
    data_dir: DataDirOpt = None,
) -> None:
    """GACOS corrections: record a request (--yes), poll it, or receive the e-mailed archive."""
    from serac.adapters.eo.gacos import GacosAdapter

    root = data_dir or get_settings().serac_data_dir
    adapter = GacosAdapter()
    ledger = JsonlManifestLedger(root / "manifest.jsonl")
    if receive is not None or poll:
        if request_id is None:
            err_console.print("[red]--receive and --poll need --request-id[/red]")
            raise typer.Exit(EXIT_USAGE)
        if poll and receive is None:
            console.print(json.dumps(adapter.poll(ledger, request_id), indent=1))
            return
        assert receive is not None
        try:
            entry = adapter.receive(ledger, request_id=request_id, url=receive, dest_root=root)
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(EXIT_USAGE) from exc
        print_entries([entry])
        return
    request = IngestRequest(
        aoi_id=aoi,
        bbox_4326=resolve_bbox(aoi, bbox, root),
        params={"dates": list(date or []), "time_utc": time_utc},
    )
    if dry_run == yes:
        err_console.print("[red]pass exactly one of --dry-run or --yes[/red]")
        raise typer.Exit(EXIT_USAGE)
    try:
        plan = adapter.plan(request)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_USAGE) from exc
    print_plan(plan)
    if dry_run:
        console.print("[dim]dry run: nothing written, no ledger entry.[/dim]")
        return
    try:
        entry = adapter.request(plan, ledger=ledger, request_id=request_id)
    except CredentialsMissingError as exc:
        err_console.print(f"[red]credentials missing:[/red] {exc}")
        raise typer.Exit(EXIT_CREDENTIALS) from exc
    print_entries([entry])
    console.print(f"request_id: [bold]{entry.params['request_id']}[/bold]")
