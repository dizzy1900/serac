"""`serac cube build` and `serac cube describe`.

Wire into `serac.cli` with `app.add_typer(cli_cube.app, name="cube")`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.adapters.storage.zarr_store import open_cube
from serac.cli_ingest import parse_bbox, parse_date
from serac.pipelines.build_cube import (
    CUBE_DIRNAME,
    REQUIRED_LAYERS,
    CubeBuildReport,
    build_cube,
    resolve_cube_aoi,
    select_entries,
)
from serac.settings import get_settings

app = typer.Typer(
    name="cube",
    help="Build and inspect per-AOI feature cubes (Zarr v3 + STAC).",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

EXIT_USAGE = 2
EXIT_BUILD_FAILED = 6

AoiOpt = Annotated[str, typer.Option("--aoi", help="AOI id, e.g. chamoli-rishiganga")]
DataDirOpt = Annotated[
    Path | None, typer.Option("--data-dir", help="Defaults to SERAC_DATA_DIR (data/).")
]


def repo_root_for(data_dir: Path) -> Path:
    """The repository root is the parent of the data directory (`data/`)."""
    return data_dir.resolve().parent


def print_report(report: CubeBuildReport) -> None:
    table = Table(
        title=f"cube {report.aoi_id}: {report.n_times} time step(s), {len(report.layers)} layers"
    )
    table.add_column("layer")
    table.add_column("status")
    table.add_column("provenance")
    table.add_column("source", overflow="fold")
    table.add_column("products", justify="right")
    table.add_column("valid t", justify="right")
    table.add_column("finite", justify="right")
    for layer in report.layers:
        table.add_row(
            layer.name,
            layer.status,
            layer.provenance,
            layer.source,
            str(len(layer.product_ids)),
            str(layer.n_times_valid) if "time" in layer.dims else "-",
            f"{layer.finite_fraction:.2f}",
        )
    console.print(table)
    grid = report.grid
    console.print(
        f"grid EPSG:{grid.epsg} {grid.width} x {grid.height} px at {grid.resolution_m:g} m, "
        f"origin ({grid.x_min:.0f}, {grid.y_max:.0f})"
        + (" [committed grid.json]" if report.committed_grid else " [recomputed from bbox]")
    )
    console.print(f"cube: {report.cube_path}  stac: {report.stac_path} ({report.stac_items} items)")
    console.print(
        f"contains_synthetic: [bold]{str(report.contains_synthetic).lower()}[/bold]  "
        f"entries considered: {report.entries_considered}  built in {report.duration_s:.1f} s"
    )
    for w in report.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")


@app.command("build")
def build(
    aoi: AoiOpt,
    from_: Annotated[str, typer.Option("--from", help="Window start (ISO date).")],
    to: Annotated[str, typer.Option("--to", help="Window end (ISO date, inclusive).")],
    raw_root: Annotated[
        Path | None,
        typer.Option("--raw-root", help="Ledger paths under this root feed the cube (data/raw)."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory (data/features/<aoi>).")
    ] = None,
    bbox: Annotated[
        str | None, typer.Option("--bbox", help="W,S,E,N degrees; overrides data/aoi/<id>.")
    ] = None,
    epsg: Annotated[
        int | None, typer.Option("--epsg", help="Cube CRS; overrides data/aoi/<id>.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the grid and the entries; write nothing.")
    ] = False,
    no_synthetic: Annotated[
        bool, typer.Option("--no-synthetic", help="Exclude labelled synthetic placeholders.")
    ] = False,
    data_dir: DataDirOpt = None,
) -> None:
    """Build data/features/<aoi>/cube.zarr, its STAC catalogue and reports/cube/<aoi>.json."""
    root = (data_dir or get_settings().serac_data_dir).resolve()
    repo_root = repo_root_for(root)
    try:
        target = resolve_cube_aoi(root, aoi, bbox=parse_bbox(bbox) if bbox else None, epsg=epsg)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_USAGE) from exc
    t0, t1 = parse_date(from_), parse_date(to, end=True)
    raw = (raw_root or (root / "raw")).resolve()
    out_dir = (out or (root / "features" / aoi)).resolve()
    ledger = JsonlManifestLedger(root / "manifest.jsonl")
    if dry_run:
        grid = target.grid
        rel_raw = (
            raw.relative_to(repo_root).as_posix()
            if raw.is_relative_to(repo_root)
            else raw.as_posix()
        )
        entries = select_entries(
            ledger, aoi, raw_root_rel=rel_raw, include_synthetic=not no_synthetic
        )
        console.print(
            f"grid EPSG:{grid.epsg} {grid.width} x {grid.height} px at {grid.resolution_m:g} m "
            + ("[committed]" if target.committed_grid else "[recomputed]")
        )
        console.print(f"window {t0.isoformat()} .. {t1.isoformat()}; raw root {rel_raw}")
        table = Table(title=f"{len(entries)} ledger entries would be considered")
        table.add_column("source")
        table.add_column("status")
        table.add_column("product")
        table.add_column("path", overflow="fold")
        for e in entries:
            table.add_row(e.source.value, e.status.value, e.product_id, e.path or "-")
        console.print(table)
        console.print("[dim]dry run: nothing written.[/dim]")
        return
    try:
        report = build_cube(
            target,
            t0,
            t1,
            raw_root=raw,
            ledger=ledger,
            out=out_dir,
            repo_root=repo_root,
            include_synthetic=not no_synthetic,
        )
    except Exception as exc:
        err_console.print(f"[red]cube build failed:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(EXIT_BUILD_FAILED) from exc
    print_report(report)


@app.command("describe")
def describe(
    aoi: AoiOpt,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Cube store (default data/features/<aoi>/cube.zarr)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    data_dir: DataDirOpt = None,
) -> None:
    """Print the layer table, grid and provenance of a built cube."""
    root = (data_dir or get_settings().serac_data_dir).resolve()
    store = path or (root / "features" / aoi / CUBE_DIRNAME)
    if not store.exists():
        err_console.print(
            f"[red]no cube at {store}; run `serac cube build --aoi {aoi}` first[/red]"
        )
        raise typer.Exit(EXIT_USAGE)
    ds = open_cube(store)
    rows: list[dict[str, object]] = []
    names = [str(n) for n in ds.data_vars if not str(n).endswith("_valid")]
    ordered = [n for n in REQUIRED_LAYERS if n in names] + sorted(
        n for n in names if n not in REQUIRED_LAYERS
    )
    for layer in ordered:
        da = ds[layer]
        values = da.values
        if np.issubdtype(values.dtype, np.floating):
            finite = float(np.isfinite(values).mean()) if values.size else 0.0
        else:
            finite = float((values != 255).mean()) if values.size else 0.0
        flag = f"{layer}_valid"
        n_valid = int(ds[flag].values.sum()) if flag in ds.data_vars else None
        rows.append(
            {
                "layer": layer,
                "dims": list(map(str, da.dims)),
                "dtype": str(da.dtype),
                "status": da.attrs.get("status"),
                "provenance": da.attrs.get("provenance"),
                "source": da.attrs.get("source"),
                "product_ids": list(da.attrs.get("product_ids", [])),
                "n_times_valid": n_valid,
                "finite_fraction": finite,
                "units": da.attrs.get("units"),
                "retrieved_at": da.attrs.get("retrieved_at"),
            }
        )
    times = (
        [str(t) for t in ds["time"].values.astype("datetime64[s]")] if "time" in ds.coords else []
    )
    summary = {
        "aoi_id": ds.attrs.get("aoi_id"),
        "grid": ds.attrs.get("grid"),
        "contains_synthetic": ds.attrs.get("contains_synthetic"),
        "cube_schema_version": ds.attrs.get("cube_schema_version"),
        "zarr_format": ds.attrs.get("zarr_format"),
        "built_at": ds.attrs.get("built_at"),
        "times": times,
        "layers": rows,
    }
    if as_json:
        console.print_json(json.dumps(summary, default=str))
        return
    grid = ds.attrs.get("grid", {})
    console.print(
        f"[bold]{summary['aoi_id']}[/bold]  EPSG:{grid.get('epsg')} {grid.get('width')} x "
        f"{grid.get('height')} px at {grid.get('resolution_m')} m  built {summary['built_at']}"
    )
    console.print(
        f"time steps: {len(times)} {times}  contains_synthetic: "
        f"[bold]{str(summary['contains_synthetic']).lower()}[/bold]"
    )
    table = Table(title=f"layers in {store}")
    table.add_column("layer")
    table.add_column("dims")
    table.add_column("dtype")
    table.add_column("status")
    table.add_column("provenance")
    table.add_column("source", overflow="fold")
    table.add_column("products", justify="right")
    table.add_column("valid t", justify="right")
    table.add_column("finite", justify="right")
    table.add_column("units")
    for r in rows:
        table.add_row(
            str(r["layer"]),
            ",".join(r["dims"]),  # type: ignore[arg-type]
            str(r["dtype"]),
            str(r["status"]),
            str(r["provenance"]),
            str(r["source"]),
            str(len(r["product_ids"])),  # type: ignore[arg-type]
            "-" if r["n_times_valid"] is None else str(r["n_times_valid"]),
            f"{r['finite_fraction']:.2f}",
            str(r["units"]),
        )
    console.print(table)
    for r in rows:
        if r["provenance"] == "synthetic":
            console.print(
                f"[yellow]{r['layer']}: SYNTHETIC placeholder, not an observation[/yellow]"
            )
        if r["status"] == "not_fetched":
            console.print(f"[dim]{r['layer']}: not_fetched (all NaN)[/dim]")
