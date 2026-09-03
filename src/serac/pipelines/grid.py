"""The fixed 30 m raster grid of an AOI's feature cube (`GridSpec` from `domain/geo.py`).

`grid_from_bbox` projects a WGS 84 bbox into the AOI's UTM zone, grows it by an optional
buffer and snaps the outer pixel edges to whole multiples of the resolution, so any two runs
over the same AOI produce the same grid and pixel centres never drift. The committed
`data/aoi/<id>/grid.json` (built by the domain lane) is the reference; `validate-cube`
recomputes and compares.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from affine import Affine
from numpy.typing import NDArray
from pyproj import Transformer

from serac.domain.geo import AOI, Bbox4326, GridSpec, check_bbox_4326

DEFAULT_RESOLUTION_M = 30.0
GRID_FILENAME = "grid.json"
AOI_FILENAME = "aoi.json"
EDGE_SAMPLES = 32
"""Points per bbox edge when projecting; keeps the envelope honest in curved UTM space."""


def _densified_edges(bbox: Bbox4326, n: int = EDGE_SAMPLES) -> tuple[list[float], list[float]]:
    w, s, e, n_ = bbox
    t = np.linspace(0.0, 1.0, n)
    xs = np.concatenate([w + (e - w) * t, np.full(n, e), e - (e - w) * t, np.full(n, w)])
    ys = np.concatenate([np.full(n, s), s + (n_ - s) * t, np.full(n, n_), n_ - (n_ - s) * t])
    return xs.tolist(), ys.tolist()


def projected_bounds(bbox: Bbox4326, epsg: int) -> tuple[float, float, float, float]:
    """Envelope of the densified bbox outline in `epsg` (metres)."""
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = _densified_edges(bbox)
    px, py = tf.transform(xs, ys)
    return float(min(px)), float(min(py)), float(max(px)), float(max(py))


def grid_from_bbox(
    aoi_id: str,
    epsg: int,
    bbox_4326: Bbox4326,
    resolution: float = DEFAULT_RESOLUTION_M,
    *,
    buffer_m: float = 0.0,
) -> GridSpec:
    """A `GridSpec` whose outer edges are snapped outward to multiples of `resolution`."""
    if resolution <= 0:
        raise ValueError("resolution must be > 0")
    if buffer_m < 0:
        raise ValueError("buffer_m must be >= 0")
    check_bbox_4326(bbox_4326)
    x0, y0, x1, y1 = projected_bounds(bbox_4326, epsg)
    x0, y0, x1, y1 = x0 - buffer_m, y0 - buffer_m, x1 + buffer_m, y1 + buffer_m
    x_min = math.floor(x0 / resolution) * resolution
    y_min = math.floor(y0 / resolution) * resolution
    x_max = math.ceil(x1 / resolution) * resolution
    y_max = math.ceil(y1 / resolution) * resolution
    width = round((x_max - x_min) / resolution)
    height = round((y_max - y_min) / resolution)
    return GridSpec(
        aoi_id=aoi_id,
        epsg=epsg,
        resolution_m=resolution,
        x_min=x_min,
        y_min=y_min,
        x_max=x_min + width * resolution,
        y_max=y_min + height * resolution,
        width=width,
        height=height,
    )


def to_affine(grid: GridSpec) -> Affine:
    """North-up GDAL affine: pixel (col, row) -> upper-left corner coordinates."""
    r = grid.resolution_m
    return Affine(r, 0.0, grid.x_min, 0.0, -r, grid.y_max)


def grid_coords(grid: GridSpec) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Pixel-centre coordinates: `x` ascending, `y` descending (row 0 is the north edge)."""
    r = grid.resolution_m
    x = grid.x_min + r * (np.arange(grid.width, dtype=np.float64) + 0.5)
    y = grid.y_max - r * (np.arange(grid.height, dtype=np.float64) + 0.5)
    return x, y


def grid_bounds_4326(grid: GridSpec) -> Bbox4326:
    """WGS 84 envelope of the grid's outer edges (densified so curvature is included)."""
    tf = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    t = np.linspace(0.0, 1.0, EDGE_SAMPLES)
    xs = np.concatenate(
        [
            grid.x_min + (grid.x_max - grid.x_min) * t,
            np.full(EDGE_SAMPLES, grid.x_max),
            grid.x_max - (grid.x_max - grid.x_min) * t,
            np.full(EDGE_SAMPLES, grid.x_min),
        ]
    )
    ys = np.concatenate(
        [
            np.full(EDGE_SAMPLES, grid.y_min),
            grid.y_min + (grid.y_max - grid.y_min) * t,
            np.full(EDGE_SAMPLES, grid.y_max),
            grid.y_max - (grid.y_max - grid.y_min) * t,
        ]
    )
    lon, lat = tf.transform(xs.tolist(), ys.tolist())
    return (float(min(lon)), float(min(lat)), float(max(lon)), float(max(lat)))


def grids_equal(a: GridSpec, b: GridSpec, *, tol_m: float = 1e-6) -> bool:
    return (
        a.aoi_id == b.aoi_id
        and a.epsg == b.epsg
        and a.width == b.width
        and a.height == b.height
        and abs(a.resolution_m - b.resolution_m) <= tol_m
        and abs(a.x_min - b.x_min) <= tol_m
        and abs(a.y_min - b.y_min) <= tol_m
        and abs(a.x_max - b.x_max) <= tol_m
        and abs(a.y_max - b.y_max) <= tol_m
    )


def grid_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "aoi" / aoi_id / GRID_FILENAME


def aoi_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "aoi" / aoi_id / AOI_FILENAME


def load_grid(path: Path) -> GridSpec:
    return GridSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_aoi(path: Path) -> AOI:
    return AOI.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_grid(grid: GridSpec, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(grid.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
