"""DEM layer from the real GLO-30 crop; slope/aspect on an analytic surface."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus
from serac.pipelines.grid import grid_from_bbox
from serac.pipelines.layers._base import REQUIRED_LAYER_ATTRS
from serac.pipelines.layers.dem import DemLayerBuilder, derive_terrain, slope_aspect

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
WINDOW = (datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 2, 15, tzinfo=UTC))


@pytest.fixture(scope="module")
def dem_entries(repo_root: Path) -> list[ManifestEntry]:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    return [
        e
        for e in ledger.entries()
        if e.source is DataSource.dem_glo30
        and e.aoi_id == "chamoli-rishiganga"
        and e.status is ManifestStatus.fetched
    ]


def test_dem_layer_from_fixture_crop(repo_root: Path, dem_entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    dem = DemLayerBuilder(repo_root).build(grid, dem_entries, WINDOW)
    assert dem.dims == ("y", "x") and dem.shape == (grid.height, grid.width)
    assert dem.dtype == np.float32
    values = dem.values
    finite = np.isfinite(values)
    assert finite.mean() > 0.9  # the crop covers the bbox; the grid adds a snapped margin
    assert np.nanmin(values) > 3000 and np.nanmax(values) < 6600  # Ronti Peak area, metres
    for key in REQUIRED_LAYER_ATTRS:
        assert key in dem.attrs, key
    assert dem.attrs["status"] == "fetched" and dem.attrs["provenance"] == "real"
    assert dem.attrs["product_ids"] == ["glo30_crop_chamoli-rishiganga"]
    assert dem.attrs["manifest_entry_ids"] == [dem_entries[-1].entry_id]
    assert dem.attrs["units"] == "m" and dem.attrs["native_resolution_m"] == 30.0
    assert dem.attrs["retrieved_at"] and dem.attrs["licence"]


def test_dem_layer_empty_without_entries(repo_root: Path) -> None:
    grid = grid_from_bbox("nowhere-test", 32644, CHAMOLI)
    empty = DemLayerBuilder(repo_root).build(grid, [], WINDOW)
    assert bool(np.isnan(empty.values).all())
    assert empty.attrs["status"] == "not_fetched" and empty.attrs["provenance"] == "none"
    assert empty.attrs["product_ids"] == []
    slope, aspect = derive_terrain(empty, grid)
    assert bool(np.isnan(slope.values).all()) and aspect.attrs["status"] == "not_fetched"


def test_slope_aspect_on_a_plane() -> None:
    # z rises 1 m per 30 m pixel towards the east: slope = atan(1/30), aspect faces west (270)
    z = np.tile(np.arange(20, dtype=np.float64), (10, 1))
    slope, aspect = slope_aspect(z, 30.0)
    inner = (slice(1, -1), slice(1, -1))
    assert np.allclose(slope[inner], np.degrees(np.arctan(1 / 30)), atol=1e-4)
    assert np.allclose(aspect[inner], 270.0, atol=1e-4)
    # rising towards the south (row index grows southward): aspect faces north (0)
    z2 = np.tile(np.arange(10, dtype=np.float64)[:, None], (1, 20))
    _s2, a2 = slope_aspect(z2, 30.0)
    assert np.allclose(a2[inner], 0.0, atol=1e-4)
    flat = np.zeros((5, 5))
    s3, a3 = slope_aspect(flat, 30.0)
    assert np.all(s3 == 0) and np.isnan(a3).all()
    holed = z.copy()
    holed[4, 4] = np.nan
    s4, a4 = slope_aspect(holed, 30.0)
    assert np.isnan(s4[4, 4]) and np.isnan(a4[4, 4])


def test_derive_terrain_attrs(repo_root: Path, dem_entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    dem = DemLayerBuilder(repo_root).build(grid, dem_entries, WINDOW)
    slope, aspect = derive_terrain(dem, grid)
    assert slope.name == "slope" and aspect.name == "aspect"
    assert slope.attrs["units"] == "degree" and "Horn" in slope.attrs["processing"]
    assert slope.attrs["product_ids"] == dem.attrs["product_ids"]
    assert np.nanmin(slope.values) >= 0 and np.nanmax(slope.values) <= 90
    assert np.nanmin(aspect.values) >= 0 and np.nanmax(aspect.values) < 360
    assert np.nanmean(slope.values) > 20  # steep Himalayan terrain
