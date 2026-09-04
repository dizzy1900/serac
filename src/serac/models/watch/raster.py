"""Getting the AOI DEM onto the AOI grid, and small raster helpers the watch layer shares.

The GLO-30 crops arrive as EPSG:4326 float32 COGs at 1 arc-second. Slope, aspect and every
SAR-geometry quantity need equal metric spacing, so the DEM is warped once onto the AOI's own
`GridSpec` (a projected UTM grid at 30 m) and everything downstream works on that grid. Doing
the warp in one documented place keeps the slope-unit delineation reproducible: same input
COG, same grid, same bytes out, which is what the delineation hash certifies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

from serac.domain.geo import GridSpec
from serac.errors import SeracError

FloatArray = NDArray[np.float64]

DEM_RESAMPLING = Resampling.bilinear
"""Bilinear: the DEM is a continuous surface and cubic overshoots at cliff edges."""


class DemNotFetchedError(SeracError):
    """The AOI DEM crop is not on disk; run `serac ingest dem --aoi <id>` first."""


@dataclass(frozen=True)
class GriddedDem:
    """A DEM warped onto an AOI's `GridSpec`, with the provenance of the bytes it came from."""

    grid: GridSpec
    elevation_m: FloatArray
    source_path: str
    source_sha256: str
    resampling: str

    @property
    def transform(self) -> Affine:
        return grid_transform(self.grid)

    @property
    def valid(self) -> NDArray[np.bool_]:
        return np.asarray(np.isfinite(self.elevation_m), dtype=np.bool_)


def grid_transform(grid: GridSpec) -> Affine:
    """North-up affine transform of a `GridSpec` (row 0 is the northern edge)."""
    return Affine(grid.resolution_m, 0.0, grid.x_min, 0.0, -grid.resolution_m, grid.y_max)


def load_grid_spec(aoi_dir: Path) -> GridSpec:
    """Read `grid.json` from an AOI directory."""
    path = aoi_dir / "grid.json"
    if not path.exists():
        raise DemNotFetchedError(f"{path} is missing; the AOI has no committed grid")
    return GridSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))


def find_dem_crop(data_dir: Path, aoi_id: str) -> Path:
    """The largest GLO-30 crop fetched for `aoi_id` under `data/raw/dem_glo30/<aoi>/`.

    "Largest" because the AOI-wide crop is what the watch layer needs, whereas a source-zone
    fixture crop covering a corner of the same AOI would silently truncate every slope unit.
    """
    root = data_dir / "raw" / "dem_glo30" / aoi_id
    candidates = sorted(root.glob("**/*.tif")) if root.exists() else []
    if not candidates:
        raise DemNotFetchedError(
            f"no GLO-30 crop under {root}; run "
            f"`serac ingest dem --aoi {aoi_id} --bbox <W,S,E,N> --yes` first"
        )
    return max(candidates, key=lambda p: p.stat().st_size)


def load_gridded_dem(
    grid: GridSpec, crop_path: Path, *, sha256: str, nodata: float | None = None
) -> GriddedDem:
    """Warp a GLO-30 crop onto `grid`, returning float64 metres with NaN outside coverage."""
    destination = np.full((grid.height, grid.width), np.nan, dtype=np.float64)
    with rasterio.open(crop_path) as src:
        source = src.read(1).astype(np.float64)
        src_nodata = nodata if nodata is not None else src.nodata
        if src_nodata is not None:
            source = np.where(source == src_nodata, np.nan, source)
        reproject(
            source=source,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid_transform(grid),
            dst_crs=CRS.from_epsg(grid.epsg),
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=DEM_RESAMPLING,
        )
    return GriddedDem(
        grid=grid,
        elevation_m=destination,
        source_path=crop_path.as_posix(),
        source_sha256=sha256,
        resampling=DEM_RESAMPLING.name,
    )


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """sha256 of a file (duplicated from the storage adapter to keep models/ free of it)."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def aoi_dem(data_dir: Path, aoi_dir: Path, aoi_id: str) -> GriddedDem:
    """The AOI's DEM on its own grid: the one entry point the rest of the watch layer uses."""
    grid = load_grid_spec(aoi_dir)
    crop = find_dem_crop(data_dir, aoi_id)
    return load_gridded_dem(grid, crop, sha256=sha256_of(crop))
