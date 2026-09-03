"""Shared machinery for cube layer builders.

A `LayerBuilder` turns ledger entries (files that were actually fetched, or labelled
synthetic placeholders) into one `xarray.DataArray` on the AOI grid. Static layers have dims
`(y, x)`; temporal layers have dims `(time, y, x)` and carry a boolean `valid` coordinate on
`time` so "no acquisition" stays distinguishable from "acquired, NaN". A builder that has
nothing to build returns `build_empty(grid)`: all-NaN with `status: not_fetched`, never a
made-up value.

Every layer carries the attrs in `REQUIRED_LAYER_ATTRS`; `validate-cube` checks them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import rasterio
import xarray as xr
from numpy.typing import NDArray
from rasterio.enums import Resampling
from rasterio.warp import reproject

from serac.domain.geo import GridSpec
from serac.domain.manifest import ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.grid import grid_coords, to_affine

CUBE_SCHEMA_VERSION = "0.1.0"
VALID_SUFFIX = "_valid"
REQUIRED_LAYER_ATTRS: tuple[str, ...] = (
    "source",
    "product_ids",
    "manifest_entry_ids",
    "retrieved_at",
    "provenance",
    "status",
    "licence",
    "units",
    "processing",
    "native_resolution_m",
)


class LayerStatus(StrEnum):
    fetched = "fetched"
    partial = "partial"
    synthetic = "synthetic"
    not_fetched = "not_fetched"


class LayerProvenance(StrEnum):
    real = "real"
    synthetic = "synthetic"
    none = "none"


class LayerBuilder(Protocol):
    """One cube layer. `build` may return `build_empty(grid)` when it has no usable entry."""

    name: str

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray: ...

    def build_empty(self, grid: GridSpec) -> xr.DataArray: ...


def layer_attrs(
    *,
    source: str,
    product_ids: Sequence[str],
    manifest_entry_ids: Sequence[str],
    retrieved_at: datetime | None,
    provenance: LayerProvenance,
    status: LayerStatus,
    licence: str | None,
    units: str,
    processing: str,
    native_resolution_m: float | None,
    **extra: Any,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "source": source,
        "product_ids": list(product_ids),
        "manifest_entry_ids": list(manifest_entry_ids),
        "retrieved_at": retrieved_at.isoformat() if retrieved_at else None,
        "provenance": provenance.value,
        "status": status.value,
        "licence": licence,
        "units": units,
        "processing": processing,
        "native_resolution_m": native_resolution_m,
        "cube_schema_version": CUBE_SCHEMA_VERSION,
    }
    attrs.update(extra)
    return attrs


def empty_attrs(source: str, units: str, processing: str) -> dict[str, Any]:
    return layer_attrs(
        source=source,
        product_ids=[],
        manifest_entry_ids=[],
        retrieved_at=None,
        provenance=LayerProvenance.none,
        status=LayerStatus.not_fetched,
        licence=None,
        units=units,
        processing=processing,
        native_resolution_m=None,
        notes="no fetched product for this AOI/window in the ledger; all values are NaN",
    )


def static_array(
    grid: GridSpec, data: NDArray[Any], name: str, attrs: dict[str, Any]
) -> xr.DataArray:
    x, y = grid_coords(grid)
    return xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x}, name=name, attrs=attrs)


def temporal_array(
    grid: GridSpec,
    data: NDArray[Any],
    times: Sequence[datetime],
    name: str,
    attrs: dict[str, Any],
    *,
    valid: Sequence[bool] | None = None,
) -> xr.DataArray:
    x, y = grid_coords(grid)
    time = np.array([to_utc_naive(t) for t in times], dtype="datetime64[ns]")
    flags = np.array(list(valid) if valid is not None else [True] * len(times), dtype=bool)
    return xr.DataArray(
        data,
        dims=("time", "y", "x"),
        coords={"time": time, "y": y, "x": x, "valid": ("time", flags)},
        name=name,
        attrs=attrs,
    )


def empty_static(grid: GridSpec, name: str, attrs: dict[str, Any]) -> xr.DataArray:
    data = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    return static_array(grid, data, name, attrs)


def empty_temporal(grid: GridSpec, name: str, attrs: dict[str, Any]) -> xr.DataArray:
    data = np.full((0, grid.height, grid.width), np.nan, dtype=np.float32)
    return temporal_array(grid, data, [], name, attrs)


def to_utc_naive(when: datetime) -> np.datetime64:
    aware = when if when.tzinfo else when.replace(tzinfo=UTC)
    return np.datetime64(aware.astimezone(UTC).replace(tzinfo=None), "ns")


def entry_time(entry: ManifestEntry, *, prefer_end: bool = False) -> datetime | None:
    when = (
        (entry.time_end or entry.time_start) if prefer_end else (entry.time_start or entry.time_end)
    )
    return when.astimezone(UTC) if when else None


def resolve_path(repo_root: Path, entry: ManifestEntry) -> Path:
    if entry.path is None:
        raise ValueError(f"entry {entry.entry_id} has no path")
    p = Path(entry.path)
    return p if p.is_absolute() else repo_root / p


def usable(entries: Sequence[ManifestEntry]) -> list[ManifestEntry]:
    """Only `fetched` and `synthetic` rows contribute pixels; everything else is refused."""
    return [e for e in entries if e.status in (ManifestStatus.fetched, ManifestStatus.synthetic)]


def provenance_of(entries: Sequence[ManifestEntry]) -> tuple[LayerProvenance, LayerStatus]:
    if not entries:
        return LayerProvenance.none, LayerStatus.not_fetched
    if any(e.provenance is Provenance.synthetic for e in entries):
        return LayerProvenance.synthetic, LayerStatus.synthetic
    return LayerProvenance.real, LayerStatus.fetched


def latest_retrieved(entries: Sequence[ManifestEntry]) -> datetime | None:
    stamps = [e.retrieved_at for e in entries if e.retrieved_at is not None]
    return max(stamps).astimezone(UTC) if stamps else None


def licence_of(entries: Sequence[ManifestEntry]) -> str | None:
    licences = sorted({e.licence for e in entries})
    return "; ".join(licences) if licences else None


def reproject_to_grid(
    src: Path,
    grid: GridSpec,
    *,
    resampling: Resampling,
    dtype: str = "float32",
    dst_nodata: float = np.nan,
    src_nodata: float | None = None,
    band: int = 1,
) -> NDArray[Any]:
    """Warp band `band` of `src` onto the grid; cells outside the source get `dst_nodata`."""
    dst = np.full((grid.height, grid.width), dst_nodata, dtype=dtype)
    with rasterio.open(src) as ds:
        nodata = ds.nodata if src_nodata is None else src_nodata
        reproject(
            source=rasterio.band(ds, band),
            destination=dst,
            src_transform=ds.transform,
            src_crs=ds.crs,
            src_nodata=nodata,
            dst_transform=to_affine(grid),
            dst_crs=f"EPSG:{grid.epsg}",
            dst_nodata=dst_nodata,
            resampling=resampling,
        )
    return dst


def coverage_fraction(arr: NDArray[Any]) -> float:
    finite = np.isfinite(arr) if np.issubdtype(arr.dtype, np.floating) else arr != 255
    return float(finite.mean()) if arr.size else 0.0
