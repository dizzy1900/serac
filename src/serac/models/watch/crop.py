"""Crop every delivered interferogram onto one fixed AOI grid, so the stack is a stack.

HyP3 picks each product's UTM zone and extent from its own burst footprint, so two pairs from
the same track can differ by a pixel or two in origin. MintPy would quietly intersect such a
stack down to the common extent, and the extent would then depend on which pairs happened to
succeed — a reproducibility hole. Instead every raster is warped once, on arrival, onto the
`WatchGrid` derived from the AOI's committed `GridSpec`, so the stack's geometry is fixed by a
committed file and not by the delivery order.

Resampling is nearest-neighbour for everything. Unwrapped phase is continuous and bilinear
would be defensible for it, but `_water_mask` is categorical and `_corr` is a bounded quality
metric that must not be smoothed into optimism; using one rule for all of them keeps the
provenance statement short and true.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

from serac.adapters.eo.hyp3_burst import ExtractedProduct
from serac.domain.geo import GridSpec

CROP_RESAMPLING = Resampling.nearest
CROP_NODATA = float("nan")


@dataclass(frozen=True)
class WatchGrid:
    """The fixed grid every InSAR product is cropped to: the AOI grid at the product pixel."""

    epsg: int
    resolution_m: float
    x_min: float
    y_max: float
    width: int
    height: int

    @property
    def transform(self) -> Affine:
        return Affine(self.resolution_m, 0.0, self.x_min, 0.0, -self.resolution_m, self.y_max)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x_min,
            self.y_max - self.height * self.resolution_m,
            self.x_min + self.width * self.resolution_m,
            self.y_max,
        )

    def as_dict(self) -> dict[str, float | int]:
        return {
            "epsg": self.epsg,
            "resolution_m": self.resolution_m,
            "x_min": self.x_min,
            "y_max": self.y_max,
            "width": self.width,
            "height": self.height,
        }


def watch_grid(grid: GridSpec, pixel_m: float) -> WatchGrid:
    """Coarsen an AOI `GridSpec` to the InSAR product pixel, snapping the origin outward.

    Snapping outward (floor on the west and ceiling on the north) means the watch grid always
    contains the AOI grid, so no slope unit is clipped by a rounding choice.
    """
    if pixel_m <= 0:
        raise ValueError("pixel_m must be > 0")
    x_min = math.floor(grid.x_min / pixel_m) * pixel_m
    y_max = math.ceil(grid.y_max / pixel_m) * pixel_m
    width = math.ceil((grid.x_max - x_min) / pixel_m)
    height = math.ceil((y_max - grid.y_min) / pixel_m)
    return WatchGrid(
        epsg=grid.epsg,
        resolution_m=float(pixel_m),
        x_min=float(x_min),
        y_max=float(y_max),
        width=int(width),
        height=int(height),
    )


def crop_raster(src_path: Path, dst_path: Path, grid: WatchGrid) -> Path:
    """Warp one raster onto `grid`, writing a deflate-compressed float32 GeoTIFF."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    destination = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    with rasterio.open(src_path) as src:
        source = src.read(1).astype(np.float32)
        if src.nodata is not None:
            source = np.where(source == np.float32(src.nodata), np.float32("nan"), source)
        reproject(
            source=source,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=CRS.from_epsg(grid.epsg),
            src_nodata=CROP_NODATA,
            dst_nodata=CROP_NODATA,
            resampling=CROP_RESAMPLING,
        )
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "width": grid.width,
        "height": grid.height,
        "crs": CRS.from_epsg(grid.epsg),
        "transform": grid.transform,
        "nodata": CROP_NODATA,
        "compress": "deflate",
        "predictor": 3,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(dst_path, "w", **profile) as dst:
        dst.write(destination, 1)
    return dst_path


def make_cropper(grid: WatchGrid):  # type: ignore[no-untyped-def]
    """Return the `crop` callable `Hyp3BurstInsarAdapter.harvest` expects."""

    def crop(extracted: ExtractedProduct, dest: Path) -> ExtractedProduct:
        dest.mkdir(parents=True, exist_ok=True)
        out = ExtractedProduct(product_id=extracted.product_id)
        for raster in extracted.rasters:
            out.rasters.append(crop_raster(raster, dest / raster.name, grid))
        if extracted.metadata is not None:
            target = dest / extracted.metadata.name
            target.write_bytes(extracted.metadata.read_bytes())
            out.metadata = target
        return out

    return crop
