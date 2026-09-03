"""GLO-30 adapter: tile naming, grid snapping, plans, and reads against the committed crops."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.windows import from_bounds

from serac.adapters.eo._http import sha256_and_size
from serac.adapters.eo.dem_glo30 import (
    HALF_PIXEL_DEG,
    PIXEL_DEG,
    Glo30DemAdapter,
    buffered_bbox,
    snap_bounds_to_grid,
    tile_indices_for_bbox,
    tile_name,
    tile_url,
    tiles_for_bbox,
    window_shape,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.ports.dem import DemProvider
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

CHAMOLI = (79.68, 30.33, 79.80, 30.42)
CROPS = {
    "lhende-khola-trishuli": ("Copernicus_DSM_COG_10_N28_00_E085_00_DEM", (203, 219)),
    "chamoli-rishiganga": ("Copernicus_DSM_COG_10_N30_00_E079_00_DEM", (325, 433)),
    "blatten-lotschental": ("Copernicus_DSM_COG_10_N46_00_E007_00_DEM", (217, 325)),
}


class FakeHttp:
    def __init__(self, sizes: dict[str, int | None], payload: bytes = b"tile-bytes") -> None:
        self.sizes = sizes
        self.payload = payload
        self.streamed: list[str] = []

    def stream_to(self, url: str, dest: Path) -> tuple[str, int]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        self.streamed.append(url)
        return sha256_and_size(dest)

    def head_content_length(self, url: str) -> int | None:
        if url not in self.sizes:
            raise ConnectionError(f"offline: {url}")
        return self.sizes[url]

    def get_json(self, url: str) -> Any:
        raise ConnectionError("offline")


def local_opener(mapping: dict[str, Path]):
    def _open(url: str) -> AbstractContextManager[Any]:
        return rasterio.open(mapping[url])

    return _open


def _adapter(tmp_path: Path, **kw: Any) -> Glo30DemAdapter:
    return Glo30DemAdapter(
        settings=SeracSettings(_env_file=None),  # type: ignore[call-arg]
        repo_root=tmp_path,
        git_sha=None,
        **kw,
    )


# -- tile naming -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (30, 79, "Copernicus_DSM_COG_10_N30_00_E079_00_DEM"),
        (0, 0, "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"),
        (-1, -80, "Copernicus_DSM_COG_10_S01_00_W080_00_DEM"),
        (-34, 151, "Copernicus_DSM_COG_10_S34_00_E151_00_DEM"),
        (46, 7, "Copernicus_DSM_COG_10_N46_00_E007_00_DEM"),
        (89, -180, "Copernicus_DSM_COG_10_N89_00_W180_00_DEM"),
        (10, 180, "Copernicus_DSM_COG_10_N10_00_W180_00_DEM"),
    ],
)
def test_tile_name(lat: int, lon: int, expected: str) -> None:
    assert tile_name(lat, lon) == expected


def test_tile_name_rejects_polar_overflow() -> None:
    with pytest.raises(ValueError):
        tile_name(90, 0)


def test_tile_url() -> None:
    name = "Copernicus_DSM_COG_10_N30_00_E079_00_DEM"
    assert tile_url(name) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_N30_00_E079_00_DEM/Copernicus_DSM_COG_10_N30_00_E079_00_DEM.tif"
    )


def test_tiles_for_single_tile_bbox() -> None:
    assert tiles_for_bbox(CHAMOLI) == ["Copernicus_DSM_COG_10_N30_00_E079_00_DEM"]


def test_tiles_for_bbox_spanning_edges() -> None:
    assert tile_indices_for_bbox((79.9, 30.9, 80.1, 31.1)) == [
        (30, 79),
        (30, 80),
        (31, 79),
        (31, 80),
    ]


def test_tiles_southern_western_hemisphere() -> None:
    assert tiles_for_bbox((-80.5, -1.5, -79.5, -0.5)) == [
        "Copernicus_DSM_COG_10_S02_00_W081_00_DEM",
        "Copernicus_DSM_COG_10_S02_00_W080_00_DEM",
        "Copernicus_DSM_COG_10_S01_00_W081_00_DEM",
        "Copernicus_DSM_COG_10_S01_00_W080_00_DEM",
    ]


def test_bbox_edge_on_integer_degree_uses_half_pixel_rule() -> None:
    # 31.0 N is the centre of N30's top row; 79.0 E the centre of E079's left column.
    assert tile_indices_for_bbox((79.0, 30.5, 79.5, 31.0)) == [(30, 79)]
    # A hair beyond the pixel edge drags in the neighbour.
    assert tile_indices_for_bbox((79.0, 30.5, 79.5, 31.0 + PIXEL_DEG)) == [(30, 79), (31, 79)]
    assert tile_indices_for_bbox((79.0 - PIXEL_DEG, 30.5, 79.5, 30.9)) == [(30, 78), (30, 79)]


def test_equator_and_meridian_crossing() -> None:
    assert tile_indices_for_bbox((-0.2, -0.2, 0.2, 0.2)) == [(-1, -1), (-1, 0), (0, -1), (0, 0)]


# -- grid geometry -----------------------------------------------------------------------------


def test_snap_bounds_to_grid_grows_outward_onto_pixel_edges() -> None:
    w, s, e, n = snap_bounds_to_grid(CHAMOLI)
    assert w <= CHAMOLI[0] and s <= CHAMOLI[1] and e >= CHAMOLI[2] and n >= CHAMOLI[3]
    assert abs(((w + HALF_PIXEL_DEG) / PIXEL_DEG) - round((w + HALF_PIXEL_DEG) / PIXEL_DEG)) < 1e-6
    assert abs(((n - HALF_PIXEL_DEG) / PIXEL_DEG) - round((n - HALF_PIXEL_DEG) / PIXEL_DEG)) < 1e-6
    assert window_shape((w, s, e, n)) == (325, 433)
    assert snap_bounds_to_grid((w, s, e, n)) == (w, s, e, n)  # idempotent


def test_buffered_bbox() -> None:
    assert buffered_bbox(CHAMOLI, 0.0) == CHAMOLI
    w, _s, _e, n = buffered_bbox(CHAMOLI, 2000.0)
    assert n - CHAMOLI[3] == pytest.approx(2000 / 111_320)
    assert CHAMOLI[0] - w > 2000 / 111_320  # longitude degrees are shorter at 30 N
    with pytest.raises(ValueError):
        buffered_bbox(CHAMOLI, -1.0)
    clamped = buffered_bbox((179.99, 89.99, 180.0, 90.0), 5000.0)
    assert clamped[2] == 180.0 and clamped[3] == 90.0
    assert clamped[0] >= -180.0 and clamped[1] == pytest.approx(89.99 - 5000 / 111_320)


# -- plan (offline) ----------------------------------------------------------------------------


def test_plan_window_mode_is_offline_and_counts_pixels(tmp_path: Path) -> None:
    def explode(url: str) -> AbstractContextManager[Any]:
        raise AssertionError("plan must not open rasters")

    adapter = _adapter(tmp_path, raster_opener=explode, http=FakeHttp({}))
    request = IngestRequest(aoi_id="chamoli-rishiganga", bbox_4326=CHAMOLI, params={"buffer_m": 0})
    plan = adapter.plan(request)
    assert plan.source is DataSource.dem_glo30 and plan.adapter == "dem_glo30"
    assert len(plan.products) == 1
    crop = plan.products[0]
    assert crop.product_id == "glo30_crop_chamoli-rishiganga"
    assert crop.properties["tiles"] == ["Copernicus_DSM_COG_10_N30_00_E079_00_DEM"]
    assert crop.properties["window_shape"] == [325, 433]
    assert plan.estimated_bytes == 325 * 433 * 4 == crop.estimated_bytes
    assert "325 x 433 px" in plan.estimate_basis and "float32" in plan.estimate_basis
    assert plan.warnings == [] and plan.requires_credentials == []
    assert crop.licence_source_url and crop.licence_source_url.startswith("https://")


def test_plan_full_tiles_uses_head_and_admits_unknown(tmp_path: Path) -> None:
    name = "Copernicus_DSM_COG_10_N30_00_E079_00_DEM"
    adapter = _adapter(tmp_path, http=FakeHttp({tile_url(name): 41_700_000}))
    request = IngestRequest(
        aoi_id="chamoli-rishiganga", bbox_4326=CHAMOLI, params={"full_tiles": True}
    )
    plan = adapter.plan(request)
    assert [p.product_id for p in plan.products] == [name]
    assert plan.estimated_bytes == 41_700_000 and "HEAD" in plan.estimate_basis

    spanning = IngestRequest(
        aoi_id="x", bbox_4326=(79.9, 30.9, 80.1, 31.1), params={"full_tiles": True}
    )
    plan = adapter.plan(spanning)
    assert len(plan.products) == 4
    assert plan.estimated_bytes is None  # three HEADs failed: unknown, not a guess
    assert sum("HEAD" in w and "failed" in w for w in plan.warnings) == 3
    assert any("cannot be estimated" in w for w in plan.warnings)


def test_plan_warns_above_50_degrees(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, http=FakeHttp({}))
    plan = adapter.plan(IngestRequest(aoi_id="x", bbox_4326=(10.0, 60.0, 10.1, 60.1)))
    assert any("50 deg" in w for w in plan.warnings)


# -- reads against the committed crops ---------------------------------------------------------


@pytest.mark.parametrize("aoi_id", sorted(CROPS))
def test_committed_crop_geometry(fixtures_dir: Path, aoi_id: str) -> None:
    tile, shape = CROPS[aoi_id]
    with rasterio.open(fixtures_dir / "dem_glo30" / aoi_id / "glo30_crop.tif") as ds:
        assert ds.shape == shape
        assert ds.crs.to_epsg() == 4326 and ds.dtypes == ("float32",) and ds.count == 1
        assert ds.res == (PIXEL_DEG, PIXEL_DEG)
        assert ds.tags()["AREA_OR_POINT"] == "Point"
        assert ds.tags()["SERAC_TILES"] == tile
        assert snap_bounds_to_grid(tuple(ds.bounds)) == pytest.approx(tuple(ds.bounds))
        data = ds.read(1)
        assert np.isfinite(data).all()
        assert data.min() > -500 and data.max() < 9000  # physical bounds, not observations


def test_read_window_matches_direct_windowed_read(fixtures_dir: Path, tmp_path: Path) -> None:
    aoi_id = "chamoli-rishiganga"
    tile, _shape = CROPS[aoi_id]
    crop_path = fixtures_dir / "dem_glo30" / aoi_id / "glo30_crop.tif"
    adapter = _adapter(tmp_path, raster_opener=local_opener({tile_url(tile): crop_path}))
    assert isinstance(adapter, DemProvider)
    inner = (79.70, 30.35, 79.75, 30.40)
    window = adapter.read_window(inner)
    assert window.crs == "EPSG:4326" and window.product_ids == (tile,)
    bounds = snap_bounds_to_grid(inner)
    assert window.bounds == pytest.approx(bounds)
    assert window.shape == window_shape(bounds)
    with rasterio.open(crop_path) as ds:
        win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
        expected = ds.read(1, window=win)
        transform = ds.window_transform(win)
    assert np.array_equal(window.data, expected)
    assert window.transform == pytest.approx(tuple(transform)[:6])


def test_fetch_window_writes_cog_and_ledger(fixtures_dir: Path, tmp_path: Path) -> None:
    aoi_id = "lhende-khola-trishuli"
    tile, shape = CROPS[aoi_id]
    crop_path = fixtures_dir / "dem_glo30" / aoi_id / "glo30_crop.tif"
    adapter = _adapter(tmp_path, raster_opener=local_opener({tile_url(tile): crop_path}))
    with rasterio.open(crop_path) as ds:
        crop_bounds = tuple(ds.bounds)
        crop_data = ds.read(1)
    request = IngestRequest(aoi_id=aoi_id, bbox_4326=crop_bounds, params={"buffer_m": 0.0})
    plan = adapter.plan(request)
    assert plan.products[0].properties["window_shape"] == list(shape)
    ledger = JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")
    entries = adapter.fetch(
        plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: False
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status is ManifestStatus.fetched and entry.source is DataSource.dem_glo30
    assert entry.path == f"data/raw/dem_glo30/{aoi_id}/glo30_crop_{aoi_id}/glo30_crop.tif"
    assert entry.url == tile_url(tile) and entry.product_level == "GLO-30"
    assert entry.params["tiles"] == [tile] and entry.params["window_shape"] == list(shape)
    out = tmp_path / entry.path
    assert sha256_and_size(out) == (entry.sha256, entry.size_bytes)
    with rasterio.open(out) as ds:
        assert ds.driver == "GTiff" and ds.profile["compress"] == "deflate"
        assert np.array_equal(ds.read(1), crop_data)
        assert tuple(ds.bounds) == pytest.approx(crop_bounds)
        assert ds.tags()["SERAC_TILES"] == tile


def test_fetch_full_tiles_streams_whole_files(tmp_path: Path) -> None:
    name = "Copernicus_DSM_COG_10_N30_00_E079_00_DEM"
    http = FakeHttp({tile_url(name): 10}, payload=b"0123456789")
    adapter = _adapter(tmp_path, http=http)
    request = IngestRequest(
        aoi_id="chamoli-rishiganga", bbox_4326=CHAMOLI, params={"full_tiles": True}
    )
    ledger = JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")
    entries = adapter.fetch(
        adapter.plan(request), dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: False
    )
    assert http.streamed == [tile_url(name)]
    assert entries[0].path == f"data/raw/dem_glo30/chamoli-rishiganga/{name}/{name}.tif"
    assert entries[0].size_bytes == 10 and entries[0].params["mode"] == "full_tile"
