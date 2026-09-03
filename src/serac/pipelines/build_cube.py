"""Build one AOI's feature cube (D6): Zarr v3 store + STAC catalogue + build report.

Inputs are ledger entries only: a pixel enters the cube if, and only if, a `fetched` (or
explicitly `synthetic`) row points at a file under `raw_root` (or under
`tests/fixtures/synthetic/`). Layers with nothing to build are all-NaN with
`status: not_fetched`. The cube's time axis is the union of the imaging layers' acquisition
times; every temporal layer is aligned to it and carries a `<layer>_valid(time)` flag.
"""

from __future__ import annotations

import json
import time as _time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac import __version__
from serac.adapters.eo._base import serac_git_sha
from serac.adapters.storage.stac_catalog import StacPaths, write_stac
from serac.adapters.storage.zarr_store import ZARR_FORMAT, write_cube
from serac.domain.geo import Bbox4326, GridSpec, check_bbox_4326
from serac.domain.manifest import ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.grid import (
    aoi_path,
    grid_from_bbox,
    grid_path,
    grids_equal,
    load_aoi,
    load_grid,
)
from serac.pipelines.layers._base import (
    CUBE_SCHEMA_VERSION,
    VALID_SUFFIX,
    LayerBuilder,
    LayerStatus,
    to_utc_naive,
)
from serac.pipelines.layers.dem import DemLayerBuilder, derive_terrain
from serac.pipelines.layers.era5 import Era5T2mLayerBuilder
from serac.pipelines.layers.nisar import NisarHhLayerBuilder
from serac.pipelines.layers.s1 import S1CoherenceLayerBuilder, S1LosVelocityLayerBuilder
from serac.pipelines.layers.s2 import S2CloudLayerBuilder, S2NdsiLayerBuilder
from serac.ports.ledger import ManifestLedger

CUBE_REPORT_CONTRACT_VERSION = "0.1.0"
CUBE_DIRNAME = "cube.zarr"
STAC_DIRNAME = "stac"
SYNTHETIC_PREFIX = "tests/fixtures/synthetic/"
STATIC_LAYERS: tuple[str, ...] = ("dem", "slope", "aspect")
TEMPORAL_LAYERS: tuple[str, ...] = (
    "s1_coherence_t",
    "s1_los_velocity_t",
    "s2_ndsi_t",
    "s2_cloud_t",
    "nisar_hh_t",
    "era5_t2m_t",
)
REQUIRED_LAYERS: tuple[str, ...] = STATIC_LAYERS + TEMPORAL_LAYERS


@dataclass(frozen=True)
class CubeAoi:
    """What the builder needs to know about an AOI (from `data/aoi/<id>/` or CLI overrides)."""

    aoi_id: str
    epsg: int
    bbox_4326: Bbox4326
    grid: GridSpec
    committed_grid: bool
    """True when `grid` was read from `data/aoi/<id>/grid.json` rather than recomputed."""


def resolve_cube_aoi(
    data_dir: Path,
    aoi_id: str,
    *,
    bbox: Bbox4326 | None = None,
    epsg: int | None = None,
    resolution_m: float = 30.0,
) -> CubeAoi:
    """AOI from `data/aoi/<id>/{aoi.json,grid.json}`; `bbox`/`epsg` overrides win."""
    aoi_file = aoi_path(data_dir, aoi_id)
    grid_file = grid_path(data_dir, aoi_id)
    committed_grid: GridSpec | None = None
    if aoi_file.exists():
        aoi = load_aoi(aoi_file)
        bbox = bbox or aoi.cube_extent_bbox_4326
        epsg = epsg or aoi.cube_epsg
        committed_grid = aoi.grid
    if grid_file.exists():
        committed_grid = load_grid(grid_file)
    if bbox is None or epsg is None:
        raise ValueError(
            f"AOI {aoi_id!r}: no data/aoi/{aoi_id}/aoi.json; pass --bbox W,S,E,N and --epsg"
        )
    check_bbox_4326(bbox)
    recomputed = grid_from_bbox(aoi_id, epsg, bbox, resolution_m)
    if (
        committed_grid is not None
        and committed_grid.epsg == epsg
        and grids_equal(committed_grid, recomputed)
    ):
        return CubeAoi(aoi_id, epsg, bbox, committed_grid, committed_grid=True)
    if committed_grid is not None and committed_grid.epsg == epsg and bbox is not None:
        # A committed grid wins over a recomputation from the same bbox/EPSG so the cube
        # matches what the domain lane published; overrides that change the EPSG do not.
        return CubeAoi(aoi_id, epsg, bbox, committed_grid, committed_grid=True)
    return CubeAoi(aoi_id, epsg, bbox, recomputed, committed_grid=False)


class LayerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    provenance: str
    source: str
    product_ids: list[str]
    n_times_valid: int
    dims: list[str]
    dtype: str
    finite_fraction: float = Field(ge=0, le=1)


class CubeBuildReport(BaseModel):
    """`reports/cube/<aoi>.json`: what went into the cube and where it went."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CUBE_REPORT_CONTRACT_VERSION
    aoi_id: str
    grid: GridSpec
    committed_grid: bool
    window: tuple[AwareDatetime, AwareDatetime]
    time_start: AwareDatetime | None
    time_end: AwareDatetime | None
    n_times: int
    layers: list[LayerReport]
    contains_synthetic: bool
    cube_path: str
    stac_path: str
    stac_items: int
    zarr_format: int = ZARR_FORMAT
    cube_schema_version: str = CUBE_SCHEMA_VERSION
    raw_root: str
    entries_considered: int
    built_at: AwareDatetime
    duration_s: float = Field(ge=0)
    serac_version: str = __version__
    git_sha: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def select_entries(
    ledger: ManifestLedger,
    aoi_id: str,
    *,
    raw_root_rel: str,
    include_synthetic: bool = True,
) -> list[ManifestEntry]:
    """Rows that may contribute pixels: fetched under `raw_root_rel`, or labelled synthetic."""
    prefix = raw_root_rel.rstrip("/") + "/"
    out: list[ManifestEntry] = []
    for e in ledger.entries():
        if e.aoi_id != aoi_id or e.path is None:
            continue
        if e.status is ManifestStatus.fetched and e.provenance is Provenance.real:
            if e.path.startswith(prefix):
                out.append(e)
        elif (
            include_synthetic
            and e.status is ManifestStatus.synthetic
            and e.provenance is Provenance.synthetic
            and e.path.startswith(SYNTHETIC_PREFIX)
        ):
            out.append(e)
    return out


def _union_times(layers: Sequence[xr.DataArray]) -> np.ndarray:
    stamps: list[np.datetime64] = []
    for da in layers:
        if "time" in da.dims:
            stamps.extend(np.asarray(da["time"].values, dtype="datetime64[ns]").tolist())
    if not stamps:
        return np.array([], dtype="datetime64[ns]")
    return np.array(
        sorted(set(np.array(stamps, dtype="datetime64[ns]").tolist())), dtype="datetime64[ns]"
    )


def _align(da: xr.DataArray, times: np.ndarray) -> tuple[xr.DataArray, xr.DataArray]:
    """Reindex a temporal layer onto the cube time axis; return (data, valid flag)."""
    time_values = np.asarray(da["time"].values, dtype="datetime64[ns]")
    flag_values = (
        np.asarray(da.coords["valid"].values, dtype=bool)
        if "valid" in da.coords
        else np.ones(da.sizes["time"], dtype=bool)
    )
    valid_src = xr.DataArray(flag_values, dims=("time",), coords={"time": time_values})
    data = da.drop_vars("valid", errors="ignore")
    if np.issubdtype(data.dtype, np.integer):
        fill: Any = 255
    else:
        fill = np.nan
    aligned = data.reindex(time=times, fill_value=fill)
    aligned.attrs = dict(da.attrs)
    flags = valid_src.reindex(time=times, fill_value=False).astype(bool)
    flags.name = f"{da.name}{VALID_SUFFIX}"
    flags.attrs = {
        "long_name": f"{da.name} has an acquisition at this time step",
        "layer": str(da.name),
    }
    return aligned, flags


def _layer_report(name: str, da: xr.DataArray, flags: xr.DataArray | None) -> LayerReport:
    values = da.values
    if np.issubdtype(values.dtype, np.floating):
        finite = float(np.isfinite(values).mean()) if values.size else 0.0
    else:
        finite = float((values != 255).mean()) if values.size else 0.0
    return LayerReport(
        name=name,
        status=str(da.attrs.get("status")),
        provenance=str(da.attrs.get("provenance")),
        source=str(da.attrs.get("source")),
        product_ids=[str(p) for p in da.attrs.get("product_ids", [])],
        n_times_valid=int(flags.values.sum()) if flags is not None else 0,
        dims=[str(d) for d in da.dims],
        dtype=str(da.dtype),
        finite_fraction=finite,
    )


def build_cube(
    aoi: CubeAoi,
    t0: datetime,
    t1: datetime,
    *,
    raw_root: Path,
    ledger: ManifestLedger,
    out: Path,
    repo_root: Path,
    include_synthetic: bool = True,
    reports_dir: Path | None = None,
    builders: Sequence[LayerBuilder] | None = None,
) -> CubeBuildReport:
    """Build `out/cube.zarr`, `out/stac/` and `reports/cube/<aoi>.json`; return the report."""
    started = _time.perf_counter()
    built_at = datetime.now(tz=UTC)
    if t0.tzinfo is None or t1.tzinfo is None:
        raise ValueError("t0 and t1 must be timezone-aware (UTC)")
    if t1 < t0:
        raise ValueError("t1 must not precede t0")
    window = (t0.astimezone(UTC), t1.astimezone(UTC))
    grid = aoi.grid
    raw_root_rel = _rel(raw_root, repo_root)
    entries = select_entries(
        ledger, aoi.aoi_id, raw_root_rel=raw_root_rel, include_synthetic=include_synthetic
    )
    warnings: list[str] = []

    dem_builder = DemLayerBuilder(repo_root)
    dem = dem_builder.build(grid, entries, window)
    slope, aspect = derive_terrain(dem, grid)
    if dem.attrs.get("status") == LayerStatus.not_fetched.value:
        warnings.append("dem: no fetched GLO-30 crop for this AOI; dem/slope/aspect are NaN")

    imaging: list[LayerBuilder] = (
        list(builders)
        if builders is not None
        else [
            S1CoherenceLayerBuilder(repo_root),
            S1LosVelocityLayerBuilder(repo_root),
            S2NdsiLayerBuilder(repo_root),
            S2CloudLayerBuilder(repo_root),
            NisarHhLayerBuilder(repo_root),
        ]
    )
    built: dict[str, xr.DataArray] = {b.name: b.build(grid, entries, window) for b in imaging}
    times = _union_times(list(built.values()))
    target_times = [
        datetime.fromtimestamp(int(t.astype("datetime64[s]").astype(np.int64)), tz=UTC)
        for t in times
    ]
    era5 = Era5T2mLayerBuilder(repo_root, target_times)
    built[era5.name] = era5.build(grid, entries, window)

    variables: dict[str, xr.DataArray] = {"dem": dem, "slope": slope, "aspect": aspect}
    reports: list[LayerReport] = [
        _layer_report("dem", dem, None),
        _layer_report("slope", slope, None),
        _layer_report("aspect", aspect, None),
    ]
    contains_synthetic = False
    for name in TEMPORAL_LAYERS:
        da = built[name]
        if da.attrs.get("status") == LayerStatus.not_fetched.value:
            warnings.append(f"{name}: not_fetched (all NaN)")
        if da.attrs.get("provenance") == "synthetic":
            contains_synthetic = True
            warnings.append(f"{name}: SYNTHETIC placeholder, not an observation")
        aligned, flags = _align(da, times)
        variables[name] = aligned
        variables[str(flags.name)] = flags
        reports.append(_layer_report(name, aligned, flags))
    if dem.attrs.get("provenance") == "synthetic":
        contains_synthetic = True

    ds = xr.Dataset(variables)
    ds = ds.assign_coords(spatial_ref=xr.DataArray(0, attrs=_spatial_ref_attrs(grid)))
    ds["x"].attrs = {"units": "m", "standard_name": "projection_x_coordinate", "axis": "X"}
    ds["y"].attrs = {"units": "m", "standard_name": "projection_y_coordinate", "axis": "Y"}
    if "time" in ds.coords:
        ds["time"].attrs = {"axis": "T", "time_zone": "UTC"}
    ds.attrs = {
        "title": f"serac feature cube {aoi.aoi_id}",
        "aoi_id": aoi.aoi_id,
        "grid": json.loads(grid.model_dump_json()),
        "committed_grid": aoi.committed_grid,
        "epsg": grid.epsg,
        "resolution_m": grid.resolution_m,
        "window": [window[0].isoformat(), window[1].isoformat()],
        "contains_synthetic": contains_synthetic,
        "cube_schema_version": CUBE_SCHEMA_VERSION,
        "zarr_format": ZARR_FORMAT,
        "raw_root": raw_root_rel,
        "built_at": built_at.isoformat(),
        "serac_version": __version__,
        "serac_git_sha": serac_git_sha(str(repo_root)),
        "layers_static": list(STATIC_LAYERS),
        "layers_temporal": list(TEMPORAL_LAYERS),
        "conventions": "serac cube 0.1; temporal layers carry <layer>_valid(time)",
    }
    out.mkdir(parents=True, exist_ok=True)
    cube_path = write_cube(ds, out / CUBE_DIRNAME)
    stac: StacPaths = write_stac(
        ds,
        aoi_id=aoi.aoi_id,
        grid=grid,
        out_dir=out / STAC_DIRNAME,
        cube_href=f"../{CUBE_DIRNAME}",
        built_at=built_at,
    )
    report = CubeBuildReport(
        aoi_id=aoi.aoi_id,
        grid=grid,
        committed_grid=aoi.committed_grid,
        window=window,
        time_start=target_times[0] if target_times else None,
        time_end=target_times[-1] if target_times else None,
        n_times=len(target_times),
        layers=reports,
        contains_synthetic=contains_synthetic,
        cube_path=_rel(cube_path, repo_root),
        stac_path=_rel(stac.catalog, repo_root),
        stac_items=len(stac.items),
        raw_root=raw_root_rel,
        entries_considered=len(entries),
        built_at=built_at,
        duration_s=_time.perf_counter() - started,
        git_sha=serac_git_sha(str(repo_root)),
        warnings=warnings,
    )
    report_dir = reports_dir if reports_dir is not None else repo_root / "reports" / "cube"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{aoi.aoi_id}.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return report


def _spatial_ref_attrs(grid: GridSpec) -> dict[str, Any]:
    from pyproj import CRS

    crs = CRS.from_epsg(grid.epsg)
    return {
        "crs_wkt": crs.to_wkt(),
        "spatial_ref": crs.to_wkt(),
        "epsg_code": grid.epsg,
        "GeoTransform": f"{grid.x_min} {grid.resolution_m} 0 {grid.y_max} 0 {-grid.resolution_m}",
    }


def cube_time_axis(ds: xr.Dataset) -> list[datetime]:
    if "time" not in ds.coords:
        return []
    return [
        datetime.fromtimestamp(int(np.datetime64(t, "s").astype(np.int64)), tz=UTC)
        for t in ds["time"].values
    ]


def naive_utc(when: datetime) -> np.datetime64:
    return to_utc_naive(when)
