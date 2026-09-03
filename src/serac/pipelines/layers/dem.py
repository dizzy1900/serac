"""Static terrain layers: `dem` from the GLO-30 crop, `slope` and `aspect` derived from it."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from numpy.typing import NDArray
from rasterio.enums import Resampling

from serac.domain.geo import GridSpec
from serac.domain.manifest import DataSource, ManifestEntry
from serac.pipelines.layers._base import (
    LayerProvenance,
    LayerStatus,
    empty_attrs,
    empty_static,
    latest_retrieved,
    layer_attrs,
    licence_of,
    reproject_to_grid,
    resolve_path,
    static_array,
    usable,
)

DEM_PROCESSING = (
    "GLO-30 EPSG:4326 crop warped to the AOI grid with bilinear resampling (rasterio.warp); "
    "no vertical adjustment (EGM2008 heights as delivered)"
)


class DemLayerBuilder:
    """`dem`: the most recently fetched GLO-30 crop for the AOI, warped to the grid."""

    name = "dem"
    source = DataSource.dem_glo30

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def select(self, entries: Sequence[ManifestEntry]) -> ManifestEntry | None:
        rows = [e for e in usable(entries) if e.source is self.source and e.path]
        if not rows:
            return None
        return max(rows, key=lambda e: e.recorded_at)

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        entry = self.select(entries)
        if entry is None:
            return self.build_empty(grid)
        data = reproject_to_grid(
            resolve_path(self.repo_root, entry), grid, resampling=Resampling.bilinear
        )
        attrs = layer_attrs(
            source=self.source.value,
            product_ids=[entry.product_id],
            manifest_entry_ids=[entry.entry_id],
            retrieved_at=latest_retrieved([entry]),
            provenance=LayerProvenance.synthetic
            if entry.provenance.value == "synthetic"
            else LayerProvenance.real,
            status=LayerStatus.synthetic
            if entry.provenance.value == "synthetic"
            else LayerStatus.fetched,
            licence=licence_of([entry]),
            units="m",
            processing=DEM_PROCESSING,
            native_resolution_m=30.0,
            source_path=entry.path,
            tiles=list(entry.params.get("tiles", [])),
        )
        return static_array(grid, data, self.name, attrs)

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        return empty_static(grid, self.name, empty_attrs(self.source.value, "m", DEM_PROCESSING))


def slope_aspect(dem: NDArray[Any], resolution_m: float) -> tuple[NDArray[Any], NDArray[Any]]:
    """Horn (1981) 3x3 finite differences: slope in degrees, aspect clockwise from north.

    Rows run north to south (row 0 is the northern edge), as in the cube. NaN propagates.
    Aspect is NaN where the surface is flat (both gradients zero).
    """
    z = np.asarray(dem, dtype=np.float64)
    pad = np.pad(z, 1, mode="edge")
    a, b, c = pad[:-2, :-2], pad[:-2, 1:-1], pad[:-2, 2:]
    d, f = pad[1:-1, :-2], pad[1:-1, 2:]
    g, h, i = pad[2:, :-2], pad[2:, 1:-1], pad[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * resolution_m)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * resolution_m)  # positive southward
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    aspect = np.degrees(np.arctan2(-dzdx, dzdy))  # downslope direction, 0 = north, 90 = east
    aspect = np.where(aspect < 0, aspect + 360.0, aspect)
    flat = (dzdx == 0) & (dzdy == 0)
    aspect = np.where(flat, np.nan, aspect)
    nan = ~np.isfinite(z)
    slope = np.where(nan, np.nan, slope)
    aspect = np.where(nan, np.nan, aspect)
    return slope.astype(np.float32), aspect.astype(np.float32)


def derive_terrain(dem: xr.DataArray, grid: GridSpec) -> tuple[xr.DataArray, xr.DataArray]:
    """`slope` and `aspect` DataArrays inheriting the DEM's provenance attrs."""
    slope, aspect = slope_aspect(dem.values, grid.resolution_m)
    base = dict(dem.attrs)
    slope_attrs = {
        **base,
        "units": "degree",
        "processing": base.get("processing", "") + "; slope by Horn 3x3 finite differences",
    }
    aspect_attrs = {
        **base,
        "units": "degree",
        "processing": base.get("processing", "")
        + "; aspect by Horn 3x3 finite differences, clockwise from north, NaN where flat",
    }
    return (
        static_array(grid, slope, "slope", slope_attrs),
        static_array(grid, aspect, "aspect", aspect_attrs),
    )
