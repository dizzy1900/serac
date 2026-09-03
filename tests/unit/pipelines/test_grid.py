"""Grid snapping, affine, coordinates and the WGS 84 envelope."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from serac.domain.geo import GridSpec
from serac.pipelines.grid import (
    grid_bounds_4326,
    grid_coords,
    grid_from_bbox,
    grids_equal,
    load_grid,
    projected_bounds,
    to_affine,
    write_grid,
)

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
LHENDE = (85.51, 28.27, 85.53, 28.29)
BLATTEN = (7.78, 46.39, 7.87, 46.45)


@pytest.mark.parametrize(
    ("aoi", "epsg", "bbox"),
    [
        ("chamoli-rishiganga", 32644, CHAMOLI),
        ("lhende-khola-trishuli", 32645, LHENDE),
        ("blatten-lotschental", 32632, BLATTEN),
    ],
)
def test_grid_is_snapped_30m_and_covers_bbox(aoi: str, epsg: int, bbox: tuple[float, ...]) -> None:
    grid = grid_from_bbox(aoi, epsg, bbox)  # type: ignore[arg-type]
    assert grid.resolution_m == 30.0 and grid.epsg == epsg
    for v in (grid.x_min, grid.y_min, grid.x_max, grid.y_max):
        assert math.isclose(v / 30.0, round(v / 30.0), abs_tol=1e-9)
    assert grid.x_max - grid.x_min == grid.width * 30.0
    assert grid.y_max - grid.y_min == grid.height * 30.0
    x0, y0, x1, y1 = projected_bounds(bbox, epsg)  # type: ignore[arg-type]
    assert grid.x_min <= x0 and grid.y_min <= y0 and grid.x_max >= x1 and grid.y_max >= y1
    assert grid.x_min > x0 - 30 and grid.y_min > y0 - 30
    env = grid_bounds_4326(grid)
    assert env[0] <= bbox[0] and env[1] <= bbox[1] and env[2] >= bbox[2] and env[3] >= bbox[3]


def test_chamoli_grid_size_is_plausible() -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    # 0.12 deg of longitude at 30.4 N is ~11.5 km, 0.09 deg of latitude ~10 km
    assert 370 <= grid.width <= 400 and 325 <= grid.height <= 345
    buffered = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI, buffer_m=3000)
    assert buffered.width >= grid.width + 200 and buffered.height >= grid.height + 200


def test_affine_and_coords() -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    tf = to_affine(grid)
    assert tf.a == 30.0 and tf.e == -30.0 and tf.c == grid.x_min and tf.f == grid.y_max
    x, y = grid_coords(grid)
    assert x.shape == (grid.width,) and y.shape == (grid.height,)
    assert x[0] == grid.x_min + 15.0 and y[0] == grid.y_max - 15.0
    assert np.all(np.diff(x) == 30.0) and np.all(np.diff(y) == -30.0)
    # pixel centres map back to themselves through the affine
    col, row = ~tf * (x[3], y[2])
    assert math.isclose(col, 3.5) and math.isclose(row, 2.5)


def test_round_trip_through_json_and_equality(tmp_path: Path) -> None:
    grid = grid_from_bbox("lhende-khola-trishuli", 32645, LHENDE)
    path = write_grid(grid, tmp_path / "aoi" / "lhende-khola-trishuli" / "grid.json")
    again = load_grid(path)
    assert grids_equal(grid, again)
    shifted = again.model_copy(update={"x_min": again.x_min + 30.0, "x_max": again.x_max + 30.0})
    assert not grids_equal(grid, shifted)
    assert GridSpec.model_validate(again.model_dump()) == again


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        grid_from_bbox("x", 32644, CHAMOLI, resolution=0)
    with pytest.raises(ValueError):
        grid_from_bbox("x", 32644, CHAMOLI, buffer_m=-1)
    with pytest.raises(ValueError):
        grid_from_bbox("x", 32644, (80.0, 30.0, 79.0, 31.0))


def test_grid_bounds_use_true_projection() -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    env = grid_bounds_4326(grid)
    tf = Transformer.from_crs(32644, 4326, always_xy=True)
    lon, lat = tf.transform(grid.x_min, grid.y_min)
    assert env[0] <= lon and env[1] <= lat
