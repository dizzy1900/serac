"""`serac watch` — M3, the slope-watch kinematic anomaly layer.

The command order is the pipeline order:

    select-track    pick one Sentinel-1 relative orbit by the rule frozen in track_select.py
    plan-network    cost the SBAS network (--dry-run prints pairs, credits and bytes)
    submit-insar    submit the network to HyP3
    poll-insar      poll, download, crop to the AOI, delete the zip, ledger everything
    slope-units     delineate slope units from the GLO-30 DEM
    mintpy          run smallbaselineApp from a typed, hashed config
    aggregate       per-unit LOS velocity / acceleration / coherence loss -> watch_cube.zarr
    optical         Sentinel-2 feature tracking with a measured noise floor
    backtest        pseudo-prospective monthly walk-forward
    tiers           current tier table for an AOI

Nothing here ever prints or persists the Earthdata token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from serac.settings import get_settings

app = typer.Typer(no_args_is_help=True, help="M3 slope watch: InSAR/optical kinematic anomalies.")
console = Console()

AOI_OPT = Annotated[str, typer.Option("--aoi", help="AOI id, e.g. chamoli-rishiganga")]
DATA_DIR_OPT = Annotated[
    Path | None, typer.Option("--data-dir", help="Defaults to SERAC_DATA_DIR (data/).")
]
REPORTS_DIR_OPT = Annotated[
    Path | None, typer.Option("--reports-dir", help="Defaults to SERAC_REPORTS_DIR (reports/).")
]


def _data_dir(value: Path | None) -> Path:
    return value if value is not None else get_settings().serac_data_dir


def _reports_dir(value: Path | None) -> Path:
    return value if value is not None else get_settings().serac_reports_dir


def _aoi_dir(data_dir: Path, aoi_id: str) -> Path:
    path = data_dir / "aoi" / aoi_id
    if not path.exists():
        raise typer.BadParameter(f"no AOI directory at {path}")
    return path


def _parse_day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    return path


# -- track selection ------------------------------------------------------------------------


@app.command("select-track")
def select_track_cmd(
    aoi: AOI_OPT,
    start: Annotated[str, typer.Option("--from", help="Window start, YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--to", help="Window end, YYYY-MM-DD")],
    data_dir: DATA_DIR_OPT = None,
    reports_dir: REPORTS_DIR_OPT = None,
    listing: Annotated[
        Path | None,
        typer.Option("--listing", help="Use a committed burst listing instead of searching."),
    ] = None,
    online: Annotated[
        bool, typer.Option("--online", help="Search ASF for burst granules.")
    ] = False,
) -> None:
    """Apply the frozen selection rule to every relative orbit crossing the AOI.

    The rule is in `serac.models.watch.track_select.SELECTION_RULE` and was committed before
    this command was first run; its sha256 goes into the report so a later edit is visible.
    """
    from serac.adapters.eo.asf_bursts import (
        AsfBurstSearchClient,
        bbox_wkt,
        listing_path,
        read_listing,
        search_in_chunks,
        write_listing,
    )
    from serac.models.watch.raster import aoi_dem
    from serac.models.watch.track_select import bursts_from_features, select_track

    data = _data_dir(data_dir)
    aoi_dir = _aoi_dir(data, aoi)
    window_start, window_end = _parse_day(start), _parse_day(end)
    raw_bbox = json.loads((aoi_dir / "aoi.json").read_text())["cube_extent_bbox_4326"]
    bbox: tuple[float, float, float, float] = (
        float(raw_bbox[0]),
        float(raw_bbox[1]),
        float(raw_bbox[2]),
        float(raw_bbox[3]),
    )

    if listing is not None:
        features = read_listing(listing)
        listing_file = listing
    else:
        cached = listing_path(data, aoi, window_start, window_end)
        if cached.exists() and not online:
            features = read_listing(cached)
        elif online:
            features = search_in_chunks(
                AsfBurstSearchClient(),
                wkt=bbox_wkt(bbox),
                start=window_start,
                end=window_end,
            )
            write_listing(cached, features)
        else:
            raise typer.BadParameter(
                f"no cached listing at {cached}; pass --online to search ASF or --listing PATH"
            )
        listing_file = cached

    bursts = bursts_from_features(features)
    dem = aoi_dem(data, aoi_dir, aoi)
    selection = select_track(
        bursts, dem, aoi_id=aoi, window_start=window_start, window_end=window_end
    )

    table = Table(title=f"track selection — {aoi} {start}..{end}")
    for column in (
        "path",
        "dir",
        "swaths",
        "head",
        "inc",
        "scenes",
        "max gap d",
        "LOS sens",
        "lay+shad",
        "cover",
        "score",
        "eligible",
    ):
        table.add_column(column)
    for c in selection.candidates:
        table.add_row(
            str(c.path_number),
            c.flight_direction[:4],
            ",".join(c.subswaths),
            f"{c.heading_deg:.1f}",
            f"{c.incidence_deg:.1f}",
            str(c.n_scenes),
            f"{c.max_gap_days:.0f}",
            f"{c.los_sensitivity:.3f}",
            f"{c.layover_shadow_fraction:.3f}",
            f"{c.coverage:.3f}",
            f"{c.score:.4f}",
            "yes" if c.eligible else "; ".join(c.ineligibility_reasons),
        )
    console.print(table)
    console.print(f"selected path: {selection.selected_path}")
    console.print(selection.selected_reason)

    out = _write_json(
        _reports_dir(reports_dir) / "watch" / f"track_selection_{aoi}.json",
        selection.model_dump(mode="json") | {"burst_listing": listing_file.as_posix()},
    )
    console.print(f"wrote {out}")


# -- network planning ----------------------------------------------------------------------


@app.command("plan-network")
def plan_network_cmd(
    aoi: AOI_OPT,
    start: Annotated[str, typer.Option("--from")],
    end: Annotated[str, typer.Option("--to")],
    path_number: Annotated[
        int | None, typer.Option("--path", help="Defaults to the selected track.")
    ] = None,
    n_conn: Annotated[int, typer.Option("--n-conn")] = 2,
    max_bt_days: Annotated[float, typer.Option("--max-bt-days")] = 36.0,
    looks: Annotated[str, typer.Option("--looks")] = "20x4",
    data_dir: DATA_DIR_OPT = None,
    reports_dir: REPORTS_DIR_OPT = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the budget; write nothing.")
    ] = False,
) -> None:
    """Cost and (unless `--dry-run`) persist the SBAS network for the selected track."""
    from serac.models.watch.plan import build_network_plan

    data = _data_dir(data_dir)
    reports = _reports_dir(reports_dir)
    plan = build_network_plan(
        data_dir=data,
        reports_dir=reports,
        aoi_id=aoi,
        window_start=_parse_day(start),
        window_end=_parse_day(end),
        path_number=path_number,
        n_conn=n_conn,
        max_bt_days=max_bt_days,
        looks=looks,
    )
    b = plan.budget
    table = Table(title=f"HyP3 burst-InSAR network — {aoi} path {b.path_number}")
    table.add_column("quantity")
    table.add_column("value", justify="right")
    rows = [
        ("acquisitions (complete burst sets)", f"{b.n_acquisitions}"),
        ("bursts per acquisition", f"{b.n_bursts_per_acquisition}"),
        ("pairs (short / anchor)", f"{b.n_pairs} ({b.n_short_pairs} / {b.n_anchor_pairs})"),
        ("credits per job", f"{b.credits_per_job}"),
        ("credits total", f"{b.credits_total}"),
        (
            "transient bytes",
            "unknown" if b.transient_bytes_estimate is None else f"{b.transient_bytes_estimate:,}",
        ),
        ("retained bytes", f"{b.retained_bytes_estimate:,}"),
        (
            "peak disk",
            "unknown" if b.peak_disk_bytes_estimate is None else f"{b.peak_disk_bytes_estimate:,}",
        ),
    ]
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)
    console.print(f"transient basis: {b.transient_basis}")
    console.print(f"retained basis: {b.retained_basis}")
    for warning in b.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")

    if dry_run:
        console.print("[cyan]--dry-run: nothing written[/cyan]")
        return
    out = _write_json(
        data / "interim" / "watch" / f"network_{aoi}.json", plan.model_dump(mode="json")
    )
    console.print(f"wrote {out}")


# -- submission and harvesting ---------------------------------------------------------------


@app.command("submit-insar")
def submit_insar_cmd(
    aoi: AOI_OPT,
    data_dir: DATA_DIR_OPT = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Submit at most N new pairs.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Submit; without it nothing is sent.")] = False,
) -> None:
    """Submit the persisted network to HyP3, one job per pair, resumable."""
    from serac.models.watch.insar_jobs import submit_network

    data = _data_dir(data_dir)
    summary = submit_network(data_dir=data, aoi_id=aoi, limit=limit, dry_run=not yes)
    console.print(json.dumps(summary, indent=2, default=str))


@app.command("poll-insar")
def poll_insar_cmd(
    aoi: AOI_OPT,
    data_dir: DATA_DIR_OPT = None,
    reports_dir: REPORTS_DIR_OPT = None,
    once: Annotated[bool, typer.Option("--once", help="One pass; do not loop.")] = False,
    poll_s: Annotated[float, typer.Option("--poll-s")] = 120.0,
    timeout_s: Annotated[float, typer.Option("--timeout-s")] = 21600.0,
) -> None:
    """Poll HyP3, then download, hash, crop and delete each delivered product."""
    from serac.models.watch.insar_jobs import poll_and_harvest

    data = _data_dir(data_dir)
    summary = poll_and_harvest(
        data_dir=data,
        reports_dir=_reports_dir(reports_dir),
        aoi_id=aoi,
        once=once,
        poll_s=poll_s,
        timeout_s=timeout_s,
    )
    console.print(json.dumps(summary, indent=2, default=str))
