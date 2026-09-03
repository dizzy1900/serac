"""Zarr v3 roundtrip guard (ADR-0003): attrs, chunks, fill values and the format constant."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import xarray as xr

from serac.adapters.storage.zarr_store import (
    CHUNK_X,
    CHUNK_Y,
    ZARR_FORMAT,
    chunks_for,
    clean_attrs,
    open_cube,
    store_format,
    write_cube,
)
from serac.domain.manifest import Provenance


def sample_cube(ny: int = 40, nx: int = 50) -> xr.Dataset:
    rng = np.random.default_rng(1)
    time = np.array(["2021-01-26T05:23:11", "2021-02-10T05:23:09"], dtype="datetime64[ns]")
    ndsi = rng.random((2, ny, nx)).astype("float32")
    ndsi[0, :5, :5] = np.nan
    cloud = rng.integers(0, 12, (2, ny, nx)).astype("uint8")
    cloud[1, 0, 0] = 255
    ds = xr.Dataset(
        {
            "dem": (("y", "x"), rng.random((ny, nx)).astype("float32")),
            "s2_ndsi_t": (("time", "y", "x"), ndsi),
            "s2_cloud_t": (("time", "y", "x"), cloud),
            "s2_ndsi_t_valid": (("time",), np.array([True, True])),
            "nisar_hh_t": (("time", "y", "x"), np.full((2, ny, nx), np.nan, dtype="float32")),
            "nisar_hh_t_valid": (("time",), np.array([False, False])),
        },
        coords={"time": time, "y": np.arange(ny, 0, -1.0), "x": np.arange(nx, dtype=float)},
        attrs={
            "contains_synthetic": False,
            "grid": {"epsg": 32644},
            "cube_schema_version": "0.1.0",
        },
    )
    ds["s2_ndsi_t"].attrs = {
        "source": "sentinel2_earthsearch",
        "product_ids": ["a", "b"],
        "retrieved_at": datetime(2026, 9, 3, tzinfo=UTC),
        "provenance": Provenance.real,
        "native_resolution_m": 10,
    }
    ds["nisar_hh_t"].attrs = {"status": "not_fetched", "product_ids": []}
    return ds


def test_roundtrip_zarr_v3(tmp_path: Path) -> None:
    assert ZARR_FORMAT == 3
    ds = sample_cube()
    path = write_cube(ds, tmp_path / "cube.zarr")
    assert store_format(path) == 3 and (path / "zarr.json").exists()
    back = open_cube(path).load()
    assert set(back.data_vars) == set(ds.data_vars)
    xr.testing.assert_allclose(back["dem"], ds["dem"])
    np.testing.assert_array_equal(
        np.isnan(back["s2_ndsi_t"].values), np.isnan(ds["s2_ndsi_t"].values)
    )
    assert bool(back["nisar_hh_t"].isnull().all())
    assert back["s2_cloud_t"].values[0, 3, 3] == ds["s2_cloud_t"].values[0, 3, 3]
    assert bool(back["s2_cloud_t"].isnull().values[1, 0, 0])  # 255 is the uint8 fill value
    assert back.attrs["contains_synthetic"] is False
    assert back.attrs["grid"] == {"epsg": 32644}
    assert back["s2_ndsi_t"].attrs["product_ids"] == ["a", "b"]
    assert back["s2_ndsi_t"].attrs["retrieved_at"] == "2026-09-03T00:00:00+00:00"
    assert back["s2_ndsi_t"].attrs["provenance"] == "real"
    assert back["nisar_hh_t"].attrs["product_ids"] == []
    assert list(back["time"].values.astype("datetime64[s]").astype(str)) == [
        "2021-01-26T05:23:11",
        "2021-02-10T05:23:09",
    ]
    assert back["s2_ndsi_t"].encoding["chunks"] == (1, 40, 50)


def test_chunks_are_clipped_to_shape() -> None:
    ds = sample_cube(ny=600, nx=700)
    assert chunks_for(ds["s2_ndsi_t"]) == (1, CHUNK_Y, CHUNK_X)
    assert chunks_for(ds["dem"]) == (CHUNK_Y, CHUNK_X)
    assert chunks_for(ds["s2_ndsi_t_valid"]) == (1,)
    small = sample_cube()
    assert chunks_for(small["dem"]) == (40, 50)


def test_clean_attrs_is_json_native() -> None:
    cleaned = clean_attrs(
        {
            "a": np.float32(1.5),
            "b": np.int64(2),
            "c": np.bool_(True),
            "d": np.array([1, 2]),
            "e": (Provenance.synthetic, Path("x/y")),
            "f": None,
            "g": {"h": datetime(2021, 1, 1, tzinfo=UTC)},
        }
    )
    assert cleaned == {
        "a": 1.5,
        "b": 2,
        "c": True,
        "d": [1, 2],
        "e": ["synthetic", "x/y"],
        "f": None,
        "g": {"h": "2021-01-01T00:00:00+00:00"},
    }
    import json

    json.dumps(cleaned)


def test_overwrite_replaces_store(tmp_path: Path) -> None:
    ds = sample_cube()
    path = write_cube(ds, tmp_path / "cube.zarr")
    smaller = ds.drop_vars(["nisar_hh_t", "nisar_hh_t_valid"])
    write_cube(smaller, path)
    assert "nisar_hh_t" not in open_cube(path).data_vars
