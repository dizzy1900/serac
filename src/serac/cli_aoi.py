"""`serac aoi ...`: build, validate and describe the AOI library in `data/aoi/`.

Exposes `app`, which `serac.cli` mounts as the `aoi` sub-command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from serac.pipelines.aoi_build import (
    FixtureOverpassClient,
    HttpxOverpassClient,
    OverpassClient,
    build_aoi,
    read_aoi_dir,
    write_aoi_dir,
)
from serac.pipelines.aoi_specs import AOI_SPECS
from serac.validation.aoi import run_suite
from serac.validation.result import print_result, write_report

app = typer.Typer(name="aoi", help="AOI library in data/aoi/.", no_args_is_help=True)

RepoOpt = Annotated[Path, typer.Option("--repo", help="Repository root.")]


@app.callback()
def _group() -> None:
    """AOI library in data/aoi/."""


def _spec(aoi_id: str) -> object:
    try:
        return AOI_SPECS[aoi_id]
    except KeyError:
        typer.echo(f"unknown AOI {aoi_id!r}; known: {', '.join(sorted(AOI_SPECS))}", err=True)
        raise typer.Exit(code=2) from None


@app.command("build")
def build(
    aoi: Annotated[str, typer.Option("--aoi", help="AOI id, e.g. lhende-khola-trishuli.")],
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Replay the committed Overpass fixture; no network."),
    ] = False,
    repo: RepoOpt = Path("."),
) -> None:
    """Build data/aoi/<id>/ from OSM hydrography (Overpass) and the sourced asset list."""
    spec = AOI_SPECS.get(aoi)
    if spec is None:
        _spec(aoi)
        return
    client: OverpassClient
    fixture = repo / spec.fixture_path
    if offline:
        if not fixture.exists():
            typer.echo(f"fixture missing: {fixture}", err=True)
            raise typer.Exit(code=2)
        client = FixtureOverpassClient(fixture)
        accessed = spec.fixture_retrieved_utc
    else:
        client = HttpxOverpassClient()
        accessed = datetime.now(tz=UTC)
    built = build_aoi(spec, client, accessed_utc=accessed)
    if not offline:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(built.raw_response)
        typer.echo(
            f"wrote raw Overpass response to {fixture} "
            f"({len(built.raw_response)} bytes, sha256 {built.report.response_sha256}); "
            "record it in data/manifest.jsonl (scripts/build_aois.py --record-ledger)"
        )
    for path in write_aoi_dir(built, repo / "data" / "aoi" / spec.id):
        typer.echo(str(path))
    r = built.report
    typer.echo(
        f"{spec.id}: centreline {r.centreline_length_km:.1f} km "
        f"({'clipped' if r.clipped else 'full'} of {r.full_path_length_km:.1f} km), "
        f"start offset {r.start_offset_m:.0f} m from the source-zone centroid"
    )
    for tid, km in r.transect_chainage_km.items():
        typer.echo(f"  transect {tid}: {km:.1f} km (node {r.transect_offset_m[tid]:.0f} m off)")


@app.command("validate")
def validate(
    repo: RepoOpt = Path("."),
    report_dir: Annotated[
        Path | None,
        typer.Option("--report-dir", help="Where to write aoi.json (default: reports/validation)."),
    ] = None,
) -> None:
    """Run the AOI validation suite over data/aoi/ (offline)."""
    result = run_suite(repo)
    print_result(result)
    out = write_report(result, report_dir or repo / "reports" / "validation")
    typer.echo(f"report: {out}")
    if not result.passed:
        raise typer.Exit(code=1)


@app.command("describe")
def describe(
    aoi: Annotated[str, typer.Option("--aoi", help="AOI id.")],
    repo: RepoOpt = Path("."),
) -> None:
    """Print the AOI's extent, grid, transects, assets and sources."""
    path = repo / "data" / "aoi" / aoi
    if not (path / "aoi.json").exists():
        typer.echo(f"no AOI directory at {path}", err=True)
        raise typer.Exit(code=2)
    files = read_aoi_dir(path)
    a = files.aoi
    typer.echo(f"{a.id}: {a.name} ({', '.join(a.countries)}), EPSG:{a.cube_epsg}")
    typer.echo(f"  extent (W,S,E,N): {a.cube_extent_bbox_4326}")
    g = files.grid
    typer.echo(
        f"  grid: {g.width} x {g.height} px @ {g.resolution_m:.0f} m, "
        f"origin ({g.x_min:.0f}, {g.y_min:.0f})"
    )
    typer.echo(f"  rivers: {', '.join(a.river_names)}")
    typer.echo("  transects:")
    for t in sorted(files.transects, key=lambda t: t.chainage_km):
        typer.echo(f"    {t.id:<24} {t.chainage_km:7.1f} km  {t.point.coordinates}")
    typer.echo("  exposed assets:")
    for x in files.assets:
        cap = f" {x.capacity_mw.best or x.capacity_mw.high} MW" if x.capacity_mw else ""
        typer.echo(
            f"    {x.id:<32} {x.asset_type.value:<16} {x.status.value:<18}{cap}  "
            f"[{x.geometry_quality.value}] sources={x.source_refs}"
        )
    typer.echo("  sources:")
    for s in a.sources:
        typer.echo(f"    {s.id:<40} {s.kind.value:<16} sha256 {s.sha256[:12]}... {s.url}")
    if a.notes:
        typer.echo(f"  notes: {a.notes}")


if __name__ == "__main__":  # pragma: no cover
    app()
