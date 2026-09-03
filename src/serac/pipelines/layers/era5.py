"""`era5_t2m_t`: ERA5 2 m temperature sampled at the cube's time steps and regridded to 30 m.

ERA5 is hourly on a 0.25 deg grid; the cube's time axis is set by the imaging layers, so the
builder takes the target times, picks the nearest ERA5 step within `max_gap` for each, and
interpolates bilinearly from the lat/lon grid to the projected pixel centres. Steps with no
ERA5 value within the tolerance are `valid = False`. Without a fetched ERA5 file (no CDS key
in the founding session) the layer is `build_empty`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator  # type: ignore[import-untyped]

from serac.domain.geo import GridSpec
from serac.domain.manifest import DataSource, ManifestEntry
from serac.pipelines.grid import grid_coords
from serac.pipelines.layers._base import (
    empty_attrs,
    empty_temporal,
    latest_retrieved,
    layer_attrs,
    licence_of,
    provenance_of,
    resolve_path,
    temporal_array,
    usable,
)

VARIABLE = "t2m"
TIME_DIMS = ("valid_time", "time")
DEFAULT_MAX_GAP = timedelta(hours=1)
PROCESSING = (
    "ERA5 single-level 2 m temperature (hourly, 0.25 deg) at the nearest hour to each cube time "
    "step, bilinearly interpolated from the lat/lon grid to the projected pixel centres"
)


def _time_dim(ds: xr.Dataset) -> str:
    for dim in TIME_DIMS:
        if dim in ds.dims:
            return dim
    raise ValueError(f"no time dimension among {TIME_DIMS} in {list(ds.dims)}")


def regrid_to_grid(field: xr.DataArray, grid: GridSpec) -> np.ndarray:
    """Bilinear lat/lon -> grid (pixel centres projected back to WGS 84)."""
    lats = np.asarray(field["latitude"].values, dtype=np.float64)
    lons = np.asarray(field["longitude"].values, dtype=np.float64)
    values = np.asarray(field.values, dtype=np.float64)
    if lats[0] > lats[-1]:
        lats, values = lats[::-1], values[::-1, :]
    if lons[0] > lons[-1]:
        lons, values = lons[::-1], values[:, ::-1]
    x, y = grid_coords(grid)
    xx, yy = np.meshgrid(x, y)
    tf = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    lon, lat = tf.transform(xx.ravel(), yy.ravel())
    interp = RegularGridInterpolator(
        (lats, lons), values, method="linear", bounds_error=False, fill_value=np.nan
    )
    out = interp(np.column_stack([lat, lon]))
    return np.asarray(out, dtype=np.float32).reshape(grid.height, grid.width)


def nearest_step(times: np.ndarray, target: datetime, max_gap: timedelta) -> int | None:
    stamps = times.astype("datetime64[ns]")
    want = np.datetime64(target.astimezone(UTC).replace(tzinfo=None), "ns")
    gaps = np.abs((stamps - want).astype("timedelta64[ns]").astype(np.int64))
    idx = int(gaps.argmin())
    return idx if gaps[idx] <= int(max_gap.total_seconds() * 1e9) else None


class Era5T2mLayerBuilder:
    name = "era5_t2m_t"
    source = DataSource.era5_cds

    def __init__(
        self,
        repo_root: Path,
        target_times: Sequence[datetime],
        *,
        max_gap: timedelta = DEFAULT_MAX_GAP,
    ) -> None:
        self.repo_root = repo_root
        self.target_times = list(target_times)
        self.max_gap = max_gap

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        rows = [e for e in usable(entries) if e.source is self.source and e.path]
        if not rows or not self.target_times:
            return self.build_empty(grid)
        datasets = [xr.open_dataset(resolve_path(self.repo_root, e)) for e in rows]
        try:
            slices: list[np.ndarray] = []
            valid: list[bool] = []
            for target in self.target_times:
                found: np.ndarray | None = None
                for ds in datasets:
                    if VARIABLE not in ds:
                        continue
                    tdim = _time_dim(ds)
                    idx = nearest_step(np.asarray(ds[tdim].values), target, self.max_gap)
                    if idx is not None:
                        found = regrid_to_grid(ds[VARIABLE].isel({tdim: idx}), grid)
                        break
                if found is None:
                    slices.append(np.full((grid.height, grid.width), np.nan, dtype=np.float32))
                    valid.append(False)
                else:
                    slices.append(found)
                    valid.append(True)
        finally:
            for ds in datasets:
                ds.close()
        provenance, status = provenance_of(rows)
        attrs = layer_attrs(
            source=self.source.value,
            product_ids=[e.product_id for e in rows],
            manifest_entry_ids=[e.entry_id for e in rows],
            retrieved_at=latest_retrieved(rows),
            provenance=provenance,
            status=status,
            licence=licence_of(rows),
            units="K",
            processing=PROCESSING,
            native_resolution_m=27_750.0,
            max_gap_hours=self.max_gap.total_seconds() / 3600.0,
            time_convention="cube time step; nearest ERA5 hour",
            notes=rows[0].notes if provenance.value == "synthetic" else None,
        )
        return temporal_array(
            grid, np.stack(slices), self.target_times, self.name, attrs, valid=valid
        )

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        return empty_temporal(grid, self.name, empty_attrs(self.source.value, "K", PROCESSING))


def open_era5(path: Path) -> Any:
    """`xarray.open_dataset` with the engine xarray picks (NetCDF-4 needs h5py; NetCDF-3 scipy)."""
    return xr.open_dataset(path)
