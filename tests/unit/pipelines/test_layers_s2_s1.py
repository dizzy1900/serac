"""Sentinel-2 layers from the real Chamoli crops; Sentinel-1 layers from HyP3 pairs.

The cube honours `raw_root` (`build_cube.select_entries`) and then prefers real fetched
pairs over labelled synthetic placeholders when both exist. On a fresh clone there are no
HyP3 burst crops (DVC); the committed synthetic pair still fills the S1 layers.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.build_cube import select_entries
from serac.pipelines.grid import grid_from_bbox
from serac.pipelines.layers._base import REQUIRED_LAYER_ATTRS
from serac.pipelines.layers.s1 import (
    S1CoherenceLayerBuilder,
    S1LosVelocityLayerBuilder,
    baseline_days,
    pairs,
)
from serac.pipelines.layers.s2 import S2CloudLayerBuilder, S2NdsiLayerBuilder, scenes

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
# the Sentinel-2 fixture window (2 560 m square) in WGS 84, so most grid pixels are covered
WINDOW_BBOX = (79.717, 30.358, 79.744, 30.381)
T0 = datetime(2021, 1, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 15, 23, 59, 59, tzinfo=UTC)
SCENES = ["S2A_44RLU_20210126_1_L2A", "S2B_44RLU_20210131_1_L2A", "S2B_44RLU_20210210_1_L2A"]
PAIR = "S1_063_20210130_20210211"
SYNTHETIC_PAIR_DIR = Path("tests/fixtures/synthetic/hyp3/chamoli-rishiganga") / PAIR
BURST_PAIR_ID = "S1_BURST_20210130_20210211"
SHA = "a" * 64


@pytest.fixture(scope="module")
def entries(repo_root: Path) -> list[ManifestEntry]:
    """Fixture-backed entries: real crops under `data/fixtures` plus labelled synthetic."""
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    return select_entries(ledger, "chamoli-rishiganga", raw_root_rel="data/fixtures")


def _burst_entry(path: Path, *, params_file: str) -> ManifestEntry:
    return ManifestEntry(
        source=DataSource.hyp3_insar,
        product_id=BURST_PAIR_ID,
        product_level="INSAR_ISCE_MULTI_BURST",
        aoi_id="chamoli-rishiganga",
        path=str(path),
        sha256=SHA,
        size_bytes=path.stat().st_size,
        retrieved_at=datetime(2026, 9, 3, tzinfo=UTC),
        licence="test double; not a HyP3 fetch",
        provenance=Provenance.real,
        status=ManifestStatus.fetched,
        time_start=datetime(2021, 1, 30, tzinfo=UTC),
        time_end=datetime(2021, 2, 11, tzinfo=UTC),
        adapter="test",
        adapter_version="0",
        params={"dt_days": 12.0, "file": params_file},
        notes="unit-test stand-in for a burst crop under raw_root; not a claimed HyP3 retrieval",
    )


def test_scene_grouping(entries: list[ManifestEntry]) -> None:
    found = scenes(entries, (T0, T1))
    assert sorted(found) == SCENES
    for bands in found.values():
        assert set(bands) == {"B03", "B11", "SCL"}
        assert all(b.status is ManifestStatus.fetched for b in bands.values())
    only_feb = scenes(entries, (datetime(2021, 2, 1, tzinfo=UTC), T1))
    assert sorted(only_feb) == SCENES[2:]


def test_ndsi_and_cloud_layers(repo_root: Path, entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    ndsi = S2NdsiLayerBuilder(repo_root).build(grid, entries, (T0, T1))
    cloud = S2CloudLayerBuilder(repo_root).build(grid, entries, (T0, T1))
    assert ndsi.dims == ("time", "y", "x") and ndsi.shape == (3, grid.height, grid.width)
    assert ndsi.dtype == np.float32 and cloud.dtype == np.uint8
    assert list(ndsi["time"].values.astype("datetime64[D]").astype(str)) == [
        "2021-01-26",
        "2021-01-31",
        "2021-02-10",
    ]
    assert ndsi.coords["valid"].values.tolist() == [True, True, True]
    finite = np.isfinite(ndsi.values)
    assert finite.mean() > 0.5  # the fixture window covers most of this small grid
    assert np.nanmin(ndsi.values) >= -1.0 and np.nanmax(ndsi.values) <= 1.0
    assert np.nanmean(ndsi.values) > 0.3  # snow-covered high terrain in winter
    scl = cloud.values
    inside = scl != 255
    assert inside.mean() > 0.5 and set(np.unique(scl[inside])) <= set(range(12))
    for key in REQUIRED_LAYER_ATTRS:
        assert key in ndsi.attrs and key in cloud.attrs, key
    assert ndsi.attrs["product_ids"] == SCENES and ndsi.attrs["status"] == "fetched"
    assert ndsi.attrs["provenance"] == "real" and ndsi.attrs["source"] == "sentinel2_earthsearch"
    assert (
        len(ndsi.attrs["manifest_entry_ids"]) == 9 and len(cloud.attrs["manifest_entry_ids"]) == 3
    )
    assert ndsi.attrs["native_resolution_m"] == 10.0 and cloud.attrs["native_resolution_m"] == 20.0
    assert len(ndsi.attrs["coverage_fraction"]) == 3
    assert cloud.attrs["scl_legend"]["11"] == "snow_or_ice"


def test_s2_layers_empty_outside_window(repo_root: Path, entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    window = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
    ndsi = S2NdsiLayerBuilder(repo_root).build(grid, entries, window)
    cloud = S2CloudLayerBuilder(repo_root).build(grid, entries, window)
    assert ndsi.sizes["time"] == 0 and cloud.sizes["time"] == 0
    assert ndsi.attrs["status"] == "not_fetched" and cloud.dtype == np.uint8


def test_synthetic_pair_layers(repo_root: Path, entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    found = pairs(entries, (T0, T1), repo_root=repo_root)
    assert [pid for pid, _ in found] == [PAIR]
    corr_entry = found[0][1]["corr"]
    assert baseline_days(corr_entry) == 12.0
    coh = S1CoherenceLayerBuilder(repo_root).build(grid, entries, (T0, T1))
    vel = S1LosVelocityLayerBuilder(repo_root).build(grid, entries, (T0, T1))
    assert coh.shape == (1, grid.height, grid.width) == vel.shape
    assert str(coh["time"].values[0].astype("datetime64[D]")) == "2021-02-11"  # secondary date
    assert coh.attrs["status"] == "synthetic" and coh.attrs["provenance"] == "synthetic"
    assert vel.attrs["status"] == "synthetic" and "SYNTHETIC" in vel.attrs["notes"]
    assert coh.attrs["product_ids"] == [PAIR] and coh.attrs["native_resolution_m"] == 80.0
    assert coh.attrs["temporal_baseline_days"] == [12.0]
    inside = np.isfinite(coh.values)
    assert inside.mean() > 0.5
    assert np.nanmin(coh.values) >= 0.0 and np.nanmax(coh.values) <= 1.0
    # LOS velocity = displacement / (12 d / 365.25 d): the +-3 cm ramp becomes ~ +-1 m/yr
    assert 0.3 < np.nanmax(np.abs(vel.values)) < 1.5
    assert vel.attrs["units"] == "m/yr"


def test_s1_layers_empty_outside_window(repo_root: Path, entries: list[ManifestEntry]) -> None:
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    window = (datetime(2021, 3, 1, tzinfo=UTC), datetime(2021, 4, 1, tzinfo=UTC))
    coh = S1CoherenceLayerBuilder(repo_root).build(grid, entries, window)
    assert coh.sizes["time"] == 0 and coh.attrs["status"] == "not_fetched"
    assert coh.attrs["source"] == DataSource.hyp3_insar.value


def test_missing_real_burst_files_fall_back_to_synthetic(
    repo_root: Path, entries: list[ManifestEntry], tmp_path: Path
) -> None:
    missing = ManifestEntry(
        source=DataSource.hyp3_insar,
        product_id=BURST_PAIR_ID,
        product_level="INSAR_ISCE_MULTI_BURST",
        aoi_id="chamoli-rishiganga",
        path=str(tmp_path / "absent_corr.tif"),
        sha256=SHA,
        size_bytes=1,
        retrieved_at=datetime(2026, 9, 3, tzinfo=UTC),
        licence="test double; file not on disk",
        provenance=Provenance.real,
        status=ManifestStatus.fetched,
        time_start=datetime(2021, 1, 30, tzinfo=UTC),
        time_end=datetime(2021, 2, 11, tzinfo=UTC),
        adapter="test",
        adapter_version="0",
        params={"dt_days": 12.0, "file": "corr"},
    )
    mixed = [*entries, missing]
    found = pairs(mixed, (T0, T1), repo_root=repo_root)
    assert [pid for pid, _ in found] == [PAIR]
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    coh = S1CoherenceLayerBuilder(repo_root).build(grid, mixed, (T0, T1))
    assert coh.attrs["status"] == "synthetic" and coh.attrs["product_ids"] == [PAIR]


def test_real_burst_pair_wins_over_synthetic(
    repo_root: Path, entries: list[ManifestEntry], tmp_path: Path
) -> None:
    src = repo_root / SYNTHETIC_PAIR_DIR
    dest = tmp_path / "data" / "raw" / "hyp3_burst_insar" / "chamoli-rishiganga" / BURST_PAIR_ID
    dest.mkdir(parents=True)
    corr = dest / f"{BURST_PAIR_ID}_corr.tif"
    los = dest / f"{BURST_PAIR_ID}_los_disp.tif"
    shutil.copy(src / f"{PAIR}_corr.tif", corr)
    shutil.copy(src / f"{PAIR}_los_disp.tif", los)
    real = [
        _burst_entry(corr, params_file="corr"),
        _burst_entry(los, params_file="los_disp"),
    ]
    mixed = [*entries, *real]
    found = pairs(mixed, (T0, T1), repo_root=repo_root)
    assert [pid for pid, _ in found] == [BURST_PAIR_ID]
    grid = grid_from_bbox("chamoli-rishiganga", 32644, WINDOW_BBOX)
    coh = S1CoherenceLayerBuilder(repo_root).build(grid, mixed, (T0, T1))
    vel = S1LosVelocityLayerBuilder(repo_root).build(grid, mixed, (T0, T1))
    assert coh.attrs["status"] == "fetched" and coh.attrs["provenance"] == "real"
    assert vel.attrs["status"] == "fetched" and vel.attrs["provenance"] == "real"
    assert coh.attrs["product_ids"] == [BURST_PAIR_ID]
    assert "SYNTHETIC" not in (vel.attrs.get("notes") or "")
