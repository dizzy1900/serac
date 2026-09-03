"""ERA5 regridding on the synthetic sample; NISAR layer is empty without granules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.grid import grid_from_bbox
from serac.pipelines.layers._base import REQUIRED_LAYER_ATTRS
from serac.pipelines.layers.era5 import Era5T2mLayerBuilder, nearest_step, regrid_to_grid
from serac.pipelines.layers.nisar import NisarHhLayerBuilder

SAMPLE_BBOX = (79.6, 30.1, 79.9, 30.4)  # inside the 79.5..80.0 x 30.0..30.5 sample cells
WINDOW = (datetime(2021, 2, 1, tzinfo=UTC), datetime(2021, 2, 28, tzinfo=UTC))


@pytest.fixture(scope="module")
def era5_entries(repo_root: Path) -> list[ManifestEntry]:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    rows = [e for e in ledger.entries() if e.source is DataSource.era5_cds]
    assert rows, "the synthetic ERA5 sample must be in the ledger"
    return rows


def test_sample_is_labelled_synthetic(repo_root: Path, era5_entries: list[ManifestEntry]) -> None:
    e = era5_entries[-1]
    assert e.status is ManifestStatus.synthetic and e.provenance is Provenance.synthetic
    assert e.path and e.path.startswith("tests/fixtures/synthetic/era5/")
    assert e.aoi_id == "synthetic-regrid-sample"  # never a real AOI id
    ds = xr.open_dataset(repo_root / e.path)
    assert ds.attrs["SERAC_PROVENANCE"] == "synthetic"
    assert ds["t2m"].dims == ("valid_time", "latitude", "longitude")
    ds.close()


def test_regrid_is_bilinear_between_cells(
    repo_root: Path, era5_entries: list[ManifestEntry]
) -> None:
    grid = grid_from_bbox("synthetic-regrid-sample", 32644, SAMPLE_BBOX, resolution=300.0)
    with xr.open_dataset(repo_root / str(era5_entries[-1].path)) as ds:
        field = ds["t2m"].isel(valid_time=0)
        out = regrid_to_grid(field, grid)
        lo, hi = float(field.min()), float(field.max())
    assert out.shape == (grid.height, grid.width) and out.dtype == np.float32
    assert np.isfinite(out).all()
    assert lo <= out.min() and out.max() <= hi
    # the sample warms eastward: the eastern column is warmer than the western one
    assert out[:, -1].mean() > out[:, 0].mean()


def test_nearest_step_tolerance() -> None:
    times = np.array(["2021-02-06T00", "2021-02-06T06"], dtype="datetime64[h]")
    assert nearest_step(times, datetime(2021, 2, 6, 5, 40, tzinfo=UTC), timedelta(hours=1)) == 1
    assert nearest_step(times, datetime(2021, 2, 6, 3, 0, tzinfo=UTC), timedelta(hours=1)) is None
    assert nearest_step(times, datetime(2021, 2, 6, 3, 0, tzinfo=UTC), timedelta(hours=3)) == 0


def test_era5_layer_on_target_times(repo_root: Path, era5_entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("synthetic-regrid-sample", 32644, SAMPLE_BBOX, resolution=300.0)
    targets = [
        datetime(2021, 2, 6, 6, 10, tzinfo=UTC),  # within 1 h of the 06:00 step
        datetime(2021, 2, 7, 12, 0, tzinfo=UTC),  # no step within tolerance
    ]
    layer = Era5T2mLayerBuilder(repo_root, targets).build(grid, era5_entries, WINDOW)
    assert layer.shape == (2, grid.height, grid.width)
    assert layer.coords["valid"].values.tolist() == [True, False]
    assert np.isfinite(layer.values[0]).all() and np.isnan(layer.values[1]).all()
    assert layer.attrs["status"] == "synthetic" and layer.attrs["provenance"] == "synthetic"
    assert layer.attrs["units"] == "K" and "SYNTHETIC" in layer.attrs["notes"]
    for key in REQUIRED_LAYER_ATTRS:
        assert key in layer.attrs, key
    empty = Era5T2mLayerBuilder(repo_root, targets).build(grid, [], WINDOW)
    assert empty.sizes["time"] == 0 and empty.attrs["status"] == "not_fetched"
    no_targets = Era5T2mLayerBuilder(repo_root, []).build(grid, era5_entries, WINDOW)
    assert no_targets.sizes["time"] == 0


def test_nisar_layer_is_not_fetched_without_granules(repo_root: Path) -> None:
    grid = grid_from_bbox("lhende-khola-trishuli", 32645, (85.51, 28.27, 85.53, 28.29))
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    rows = [e for e in ledger.entries() if e.source is DataSource.nisar_asf]
    assert rows and all(e.status is not ManifestStatus.fetched for e in rows)  # probe is `listed`
    layer = NisarHhLayerBuilder(repo_root).build(grid, rows, WINDOW)
    assert layer.sizes["time"] == 0
    assert layer.attrs["status"] == "not_fetched" and layer.attrs["provenance"] == "none"
    assert "untested" in layer.attrs["processing"]
    for key in REQUIRED_LAYER_ATTRS:
        assert key in layer.attrs, key
