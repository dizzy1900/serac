"""STAC publisher: one item per time slice, offline validation against the vendored schemas."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from serac.adapters.storage.stac_catalog import (
    STAC_VERSION,
    collection_id,
    cube_times,
    item_id,
    layers_present_at,
    read_stac,
    schema_registry,
    validate_stac,
    write_stac,
)
from serac.pipelines.grid import grid_from_bbox

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
SCHEMAS = Path(__file__).resolve().parents[3] / "fixtures" / "stac_schemas"


def tiny_cube() -> xr.Dataset:
    time = np.array(["2021-01-26T05:23:11", "2021-02-10T05:23:09"], dtype="datetime64[ns]")
    ny, nx = 4, 5
    return xr.Dataset(
        {
            "dem": (("y", "x"), np.ones((ny, nx), dtype="float32")),
            "s2_ndsi_t": (("time", "y", "x"), np.zeros((2, ny, nx), dtype="float32")),
            "s2_ndsi_t_valid": (("time",), np.array([True, True])),
            "s1_coherence_t": (("time", "y", "x"), np.zeros((2, ny, nx), dtype="float32")),
            "s1_coherence_t_valid": (("time",), np.array([False, True])),
            "nisar_hh_t": (("time", "y", "x"), np.full((2, ny, nx), np.nan, dtype="float32")),
            "nisar_hh_t_valid": (("time",), np.array([False, False])),
        },
        coords={"time": time, "y": np.arange(ny, 0, -1.0), "x": np.arange(nx, dtype=float)},
        attrs={"contains_synthetic": True, "cube_schema_version": "0.1.0", "zarr_format": 3},
    )


def test_vendored_schema_manifest_matches_files() -> None:
    manifest = json.loads((SCHEMAS / "MANIFEST.json").read_text("utf-8"))
    assert len(manifest["files"]) == 13
    for row in manifest["files"]:
        data = (SCHEMAS / row["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"], row["file"]
        assert len(data) == row["size_bytes"]
        assert row["url"].startswith("https://") and row["provenance"] == "real"
        assert row["licence"].startswith(("Apache-2.0", "MIT"))
    registry = schema_registry(SCHEMAS)
    assert registry.contents("https://schemas.stacspec.org/v1.1.0/item-spec/json-schema/item.json")
    assert registry.contents("https://geojson.org/schema/Feature.json")


def test_write_and_validate_offline(tmp_path: Path) -> None:
    ds = tiny_cube()
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    paths = write_stac(
        ds,
        aoi_id="chamoli-rishiganga",
        grid=grid,
        out_dir=tmp_path / "stac",
        cube_href="../cube.zarr",
        built_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert paths.catalog.exists() and paths.collection.exists()
    assert len(paths.items) == 2 == ds.sizes["time"]
    catalog, collection, items = read_stac(tmp_path / "stac")
    assert catalog["stac_version"] == STAC_VERSION == collection["stac_version"]
    assert collection["id"] == collection_id("chamoli-rishiganga")
    assert collection["serac:contains_synthetic"] is True
    assert (
        collection["serac:grid"]["epsg"] == 32644
        and collection["serac:grid"]["width"] == grid.width
    )
    assert collection["serac:temporal_layers"] == ["nisar_hh_t", "s1_coherence_t", "s2_ndsi_t"]
    assert collection["assets"]["cube"]["href"] == "../cube.zarr"
    assert collection["extent"]["temporal"]["interval"][0] == [
        "2021-01-26T05:23:11Z",
        "2021-02-10T05:23:09Z",
    ]
    ids = sorted(i["id"] for i in items)
    assert ids == [item_id("chamoli-rishiganga", t) for t in cube_times(ds)]
    by_id = {i["id"]: i for i in items}
    first = by_id[ids[0]]
    assert first["properties"]["serac:layers_present"] == ["s2_ndsi_t"]
    assert by_id[ids[1]]["properties"]["serac:layers_present"] == ["s1_coherence_t", "s2_ndsi_t"]
    assert first["geometry"]["type"] == "Polygon" and len(first["bbox"]) == 4
    assert layers_present_at(ds, 0) == ["s2_ndsi_t"]
    assert validate_stac(tmp_path / "stac", SCHEMAS) == []


def test_validation_catches_a_broken_item(tmp_path: Path) -> None:
    ds = tiny_cube()
    grid = grid_from_bbox("chamoli-rishiganga", 32644, CHAMOLI)
    paths = write_stac(
        ds,
        aoi_id="chamoli-rishiganga",
        grid=grid,
        out_dir=tmp_path / "stac",
        cube_href="../cube.zarr",
    )
    broken = json.loads(paths.items[0].read_text("utf-8"))
    del broken["geometry"]
    broken["properties"].pop("datetime")
    paths.items[0].write_text(json.dumps(broken), encoding="utf-8")
    problems = validate_stac(tmp_path / "stac", SCHEMAS)
    assert problems and any("geometry" in p or "datetime" in p for p in problems)
    collection = json.loads(paths.collection.read_text("utf-8"))
    collection["stac_version"] = "1.0.0"
    del collection["extent"]
    paths.collection.write_text(json.dumps(collection), encoding="utf-8")
    problems = validate_stac(tmp_path / "stac", SCHEMAS)
    assert any(p.startswith("collection:") for p in problems)


def test_no_time_axis_gives_no_items(tmp_path: Path) -> None:
    ds = tiny_cube().drop_dims("time")
    grid = grid_from_bbox("blatten-lotschental", 32632, (7.78, 46.39, 7.87, 46.45))
    paths = write_stac(
        ds,
        aoi_id="blatten-lotschental",
        grid=grid,
        out_dir=tmp_path / "stac",
        cube_href="../cube.zarr",
    )
    assert paths.items == ()
    assert validate_stac(tmp_path / "stac", SCHEMAS) == []
    with pytest.raises(FileNotFoundError):
        read_stac(tmp_path)
    (tmp_path / "catalog.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one collection"):
        read_stac(tmp_path)
