"""Online smoke: the public GLO-30 COG still serves the window the committed crop was cut from."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from tests.conftest import require_network

from serac.adapters.eo.dem_glo30 import GLO30_BUCKET_URL, Glo30DemAdapter, tile_url
from serac.ports.ingest import IngestRequest

pytestmark = pytest.mark.online

HOST = GLO30_BUCKET_URL.removeprefix("https://")
LHENDE = (85.51, 28.27, 85.53, 28.29)


def test_head_reports_tile_size() -> None:
    require_network(HOST)
    adapter = Glo30DemAdapter()
    request = IngestRequest(
        aoi_id="lhende-khola-trishuli", bbox_4326=LHENDE, params={"full_tiles": True}
    )
    plan = adapter.plan(request)
    assert [p.product_id for p in plan.products] == ["Copernicus_DSM_COG_10_N28_00_E085_00_DEM"]
    assert plan.estimated_bytes is not None and plan.estimated_bytes > 10_000_000
    assert plan.products[0].url == tile_url("Copernicus_DSM_COG_10_N28_00_E085_00_DEM")


def test_window_read_reproduces_committed_crop(fixtures_dir: Path) -> None:
    require_network(HOST)
    crop = fixtures_dir / "dem_glo30" / "lhende-khola-trishuli" / "glo30_crop.tif"
    with rasterio.open(crop) as ds:
        expected = ds.read(1)
        bounds = tuple(ds.bounds)
    window = Glo30DemAdapter().read_window(bounds)
    assert window.shape == expected.shape
    assert np.array_equal(window.data, expected), "GLO-30 bytes changed upstream: refresh fixtures"
