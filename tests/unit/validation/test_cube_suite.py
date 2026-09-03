"""validate-cube: passes on the acceptance cube; corrupted cubes fail the right rule."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.adapters.storage.zarr_store import open_cube, write_cube
from serac.pipelines.build_cube import build_cube, resolve_cube_aoi
from serac.pipelines.grid import grid_from_bbox, write_grid
from serac.validation.cube import build_fixture_cube, run_suite
from serac.validation.result import SuiteResult

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
T0 = datetime(2021, 1, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 15, 23, 59, 59, tzinfo=UTC)


@pytest.fixture(scope="module")
def built(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("validate-cube")
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    target = resolve_cube_aoi(repo_root / "data", "chamoli-rishiganga", bbox=CHAMOLI, epsg=32644)
    build_cube(
        target,
        T0,
        T1,
        raw_root=repo_root / "data" / "fixtures",
        ledger=ledger,
        out=out,
        repo_root=repo_root,
        reports_dir=out / "reports",
    )
    return out


def failed(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if c.failed}


def names(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks}


def corrupted_copy(built: Path, tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    """Copy the built cube directory, rewrite the store after `mutate(ds)`, return the store."""
    dest = tmp_path / "copy"
    shutil.copytree(built, dest)
    ds = open_cube(dest / "cube.zarr").load()
    ds = mutate(ds)
    shutil.rmtree(dest / "cube.zarr")
    write_cube(ds, dest / "cube.zarr")
    return dest / "cube.zarr"


def test_passes_on_acceptance_cube(repo_root: Path, built: Path) -> None:
    result = run_suite(repo_root, built / "cube.zarr")
    assert result.passed, [c for c in result.checks if c.failed]
    expected = {
        "cube.exists",
        "cube.zarr_format",
        "cube.crs",
        "cube.grid_matches_committed",
        "cube.resolution_30m",
        "cube.origin_snapped",
        "cube.time_monotonic_utc",
        "cube.required_layers",
        "cube.layer_attrs_complete",
        "cube.not_fetched_all_nan",
        "cube.product_ids_resolve",
        "cube.provenance_matches_ledger",
        "cube.synthetic_flag_consistent",
        "cube.valid_mask_matches_data",
        "cube.stac_items_match_time",
        "cube.stac_schema_valid",
        "cube.report_parses",
    }
    assert expected <= names(result)
    assert result.suite == "cube" and "passed" in result.summary()


def test_missing_cube_is_built_from_fixtures(repo_root: Path, tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    (fake_repo / "data").mkdir(parents=True)
    for name in ("fixtures", "manifest.jsonl"):
        src = repo_root / "data" / name
        if src.is_dir():
            shutil.copytree(src, fake_repo / "data" / name)
        else:
            shutil.copy(src, fake_repo / "data" / name)
    shutil.copytree(repo_root / "tests" / "fixtures", fake_repo / "tests" / "fixtures")
    result = run_suite(fake_repo)
    assert result.passed, [c for c in result.checks if c.failed]
    assert "cube.built_from_fixtures" in names(result)
    assert (
        fake_repo / "reports" / "cube" / "_validate" / "chamoli-rishiganga" / "cube.zarr"
    ).exists()
    assert build_fixture_cube(fake_repo).exists()
    not_built = run_suite(fake_repo, fake_repo / "nope.zarr")
    assert failed(not_built) == {"cube.exists"}


def test_corrupt_resolution_and_origin(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        grid = ds.attrs["grid"]
        x = grid["x_min"] + 7.0 + 20.0 * np.arange(grid["width"])  # 20 m spacing, off-centre
        return ds.assign_coords(x=x)

    store = corrupted_copy(built, tmp_path, mutate)
    bad = failed(run_suite(repo_root, store))
    assert {"cube.resolution_30m", "cube.origin_snapped"} <= bad


def test_corrupt_crs(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        ds.attrs["epsg"] = 32645
        return ds

    assert "cube.crs" in failed(run_suite(repo_root, corrupted_copy(built, tmp_path, mutate)))


def test_corrupt_time_order(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        return ds.assign_coords(time=ds["time"].values[::-1])

    assert "cube.time_monotonic_utc" in failed(
        run_suite(repo_root, corrupted_copy(built, tmp_path, mutate))
    )


def test_corrupt_missing_layer_and_attrs(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        ds = ds.drop_vars(["nisar_hh_t"])
        del ds["dem"].attrs["licence"]
        ds["slope"].attrs["status"] = "made_up"
        return ds

    bad = failed(run_suite(repo_root, corrupted_copy(built, tmp_path, mutate)))
    assert {"cube.required_layers", "cube.layer_attrs_complete"} <= bad


def test_corrupt_values_in_not_fetched_layer(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        values = ds["era5_t2m_t"].values.copy()
        values[0, 0, 0] = 260.0  # a fabricated observation
        ds["era5_t2m_t"].values = values
        return ds

    bad = failed(run_suite(repo_root, corrupted_copy(built, tmp_path, mutate)))
    assert "cube.not_fetched_all_nan" in bad
    assert "cube.valid_mask_matches_data" in bad  # invalid step with data


def test_corrupt_synthetic_flag_and_provenance(
    repo_root: Path, built: Path, tmp_path: Path
) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        ds.attrs["contains_synthetic"] = False
        for name in ("s1_coherence_t", "s1_los_velocity_t"):  # lying about the synthetic pair
            ds[name].attrs["provenance"] = "real"
            ds[name].attrs["status"] = "fetched"
        return ds

    bad = failed(run_suite(repo_root, corrupted_copy(built, tmp_path, mutate)))
    assert "cube.provenance_matches_ledger" in bad
    assert "cube.synthetic_flag_consistent" not in bad  # flag matches the (false) attrs now
    assert "cube.stac_synthetic_flag" in bad  # but the STAC collection still says synthetic


def test_corrupt_product_ids(repo_root: Path, built: Path, tmp_path: Path) -> None:
    def mutate(ds: xr.Dataset) -> xr.Dataset:
        ds["s2_ndsi_t"].attrs["product_ids"] = ["S2X_NOT_IN_LEDGER"]
        return ds

    assert "cube.product_ids_resolve" in failed(
        run_suite(repo_root, corrupted_copy(built, tmp_path, mutate))
    )


def test_corrupt_stac_items(repo_root: Path, built: Path, tmp_path: Path) -> None:
    dest = tmp_path / "copy"
    shutil.copytree(built, dest)
    items = sorted((dest / "stac" / "serac-cube-chamoli-rishiganga").rglob("*.json"))
    item = next(p for p in items if p.name != "collection.json")
    shutil.rmtree(item.parent)
    bad = failed(run_suite(repo_root, dest / "cube.zarr"))
    assert "cube.stac_items_match_time" in bad


def test_committed_grid_mismatch_is_reported(repo_root: Path, built: Path, tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    shutil.copytree(repo_root / "data" / "fixtures", fake_repo / "data" / "fixtures")
    shutil.copy(repo_root / "data" / "manifest.jsonl", fake_repo / "data" / "manifest.jsonl")
    shutil.copytree(repo_root / "tests" / "fixtures", fake_repo / "tests" / "fixtures")
    other = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI, buffer_m=1500)
    write_grid(other, fake_repo / "data" / "aoi" / "chamoli-rishiganga" / "grid.json")
    bad = failed(run_suite(fake_repo, built / "cube.zarr"))
    assert "cube.grid_matches_committed" in bad
    # The cube sits on the grid the AOI lane published, so that is the matching one.
    matching = resolve_cube_aoi(
        repo_root / "data", "chamoli-rishiganga", bbox=CHAMOLI, epsg=32644
    ).grid
    write_grid(matching, fake_repo / "data" / "aoi" / "chamoli-rishiganga" / "grid.json")
    good = run_suite(fake_repo, built / "cube.zarr")
    assert "cube.grid_matches_committed" not in failed(good)


def test_report_written_is_valid_json(built: Path) -> None:
    report = json.loads((built / "reports" / "chamoli-rishiganga.json").read_text("utf-8"))
    assert report["n_times"] == 4 and report["contains_synthetic"] is True
    assert np.isfinite(report["duration_s"])
