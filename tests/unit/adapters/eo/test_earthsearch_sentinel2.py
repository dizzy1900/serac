"""Earth Search Sentinel-2 adapter against the committed STAC items and band crops."""

from __future__ import annotations

import copy
import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio

from serac.adapters.eo._http import sha256_and_size
from serac.adapters.eo.earthsearch_sentinel2 import (
    BAND_ASSETS,
    EarthSearchSentinel2Adapter,
    item_to_candidate,
    snap_bounds,
    utm_bounds,
    window_pixels,
)
from serac.adapters.eo.s2_cloud import cloud_fraction
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "chamoli-rishiganga"
BBOX = (79.68, 30.33, 79.80, 30.42)
WINDOW = (376680.0, 3359180.0, 379240.0, 3361740.0)  # the committed fixture window, EPSG:32644
SCENES = ["S2A_44RLU_20210126_1_L2A", "S2B_44RLU_20210131_1_L2A", "S2B_44RLU_20210210_1_L2A"]
T0 = datetime(2021, 1, 15, tzinfo=UTC)
T1 = datetime(2021, 2, 20, 23, 59, 59, tzinfo=UTC)


def load_items(fixtures_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads((fixtures_dir / "sentinel2" / AOI / sid / "item.json").read_text("utf-8"))
        for sid in SCENES
    ]


class FakeStac:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def search_items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        max_cloud = kwargs.get("max_cloud")
        out = [copy.deepcopy(i) for i in self.items]
        if max_cloud is not None:
            out = [i for i in out if i["properties"]["eo:cloud_cover"] <= max_cloud]
        return out


def local_opener(fixtures_dir: Path, items: list[dict[str, Any]]):
    mapping: dict[str, Path] = {}
    for item in items:
        for stem, key in BAND_ASSETS.items():
            mapping[item["assets"][key]["href"]] = (
                fixtures_dir / "sentinel2" / AOI / item["id"] / f"{stem}.tif"
            )

    def _open(url: str) -> AbstractContextManager[Any]:
        return rasterio.open(mapping[url])

    return _open


@pytest.fixture
def items(fixtures_dir: Path) -> list[dict[str, Any]]:
    return load_items(fixtures_dir)


@pytest.fixture
def adapter(
    fixtures_dir: Path, items: list[dict[str, Any]], tmp_path: Path
) -> EarthSearchSentinel2Adapter:
    return EarthSearchSentinel2Adapter(
        FakeStac(items),
        raster_opener=local_opener(fixtures_dir, items),
        settings=SeracSettings(_env_file=None),  # type: ignore[call-arg]
        repo_root=tmp_path,
        git_sha=None,
    )


def _request(**params: Any) -> IngestRequest:
    return IngestRequest(
        aoi_id=AOI,
        bbox_4326=BBOX,
        time_start=T0,
        time_end=T1,
        event_id="chamoli-2021",
        params=params,
    )


# -- geometry helpers --------------------------------------------------------------------------


def test_utm_bounds_and_snap() -> None:
    w, s, e, n = utm_bounds(BBOX, 32644)
    assert 370_000 < w < e < 390_000 and 3_355_000 < s < n < 3_370_000
    snapped = snap_bounds((w, s, e, n), origin=(300000.0, 3400020.0))
    assert snapped[0] <= w and snapped[1] <= s and snapped[2] >= e and snapped[3] >= n
    for v in (
        snapped[0] - 300000.0,
        snapped[2] - 300000.0,
        3400020.0 - snapped[1],
        3400020.0 - snapped[3],
    ):
        assert v % 20 == 0
    assert snap_bounds(WINDOW, origin=(300000.0, 3400020.0)) == WINDOW
    assert window_pixels(WINDOW, 10) == (256, 256) and window_pixels(WINDOW, 20) == (128, 128)


# -- search ------------------------------------------------------------------------------------


def test_search_maps_items_to_products(adapter: EarthSearchSentinel2Adapter) -> None:
    products = adapter.search(_request())
    assert [p.product_id for p in products] == SCENES  # sorted by acquisition
    call = adapter.stac.calls[0]  # type: ignore[attr-defined]
    assert call["collection"] == "sentinel-2-l2a" and call["bbox"] == BBOX
    assert call["datetime_range"] == "2021-01-15T00:00:00Z/2021-02-20T23:59:59Z"
    assert call["max_cloud"] == 40.0 and call["limit"] == 200
    post = products[-1]
    assert post.source is DataSource.sentinel2_earthsearch and post.product_level == "L2A"
    assert sorted(post.assets) == ["B03", "B11", "SCL"]
    assert post.assets["SCL"].endswith("/S2B_44RLU_20210210_1_L2A/SCL.tif")
    assert post.url is not None and post.url.endswith("/items/S2B_44RLU_20210210_1_L2A")
    assert post.time_start == datetime(2021, 2, 10, 5, 30, 31, 60000, tzinfo=UTC)
    assert post.properties["proj:epsg"] == 32644
    assert post.properties["eo:cloud_cover"] == pytest.approx(8.167754)
    assert post.properties["stac_item"]["id"] == "S2B_44RLU_20210210_1_L2A"
    assert post.bbox_4326 is not None and post.bbox_4326[0] < 79.68 < 79.80 < post.bbox_4326[2]


def test_search_requires_a_time_window(adapter: EarthSearchSentinel2Adapter) -> None:
    with pytest.raises(ValueError, match="time_start"):
        adapter.search(IngestRequest(aoi_id=AOI, bbox_4326=BBOX))


def test_search_collapses_reprocessings_unless_asked(
    fixtures_dir: Path, items: list[dict[str, Any]], tmp_path: Path
) -> None:
    older = copy.deepcopy(items[2])
    older["id"] = "S2B_44RLU_20210210_0_L2A"
    older["properties"]["s2:processing_baseline"] = "02.14"
    older["properties"]["eo:cloud_cover"] = 13.819823
    adapter = EarthSearchSentinel2Adapter(
        FakeStac([*items, older]), repo_root=tmp_path, git_sha=None
    )
    ids = [p.product_id for p in adapter.search(_request())]
    assert ids == SCENES  # the 05.00 reprocessing wins over 02.14
    ids = [p.product_id for p in adapter.search(_request(keep_reprocessings=True))]
    assert "S2B_44RLU_20210210_0_L2A" in ids and len(ids) == 4


def test_search_max_scenes_ranks_by_tile_cloud(
    adapter: EarthSearchSentinel2Adapter, items: list[dict[str, Any]]
) -> None:
    best = min(items, key=lambda i: i["properties"]["eo:cloud_cover"])["id"]
    products = adapter.search(_request(max_scenes=1))
    assert [p.product_id for p in products] == [best]
    products = adapter.search(_request(max_cloud=0.01))
    assert all(p.properties["eo:cloud_cover"] <= 0.01 for p in products)


def test_item_to_candidate(items: list[dict[str, Any]]) -> None:
    c = item_to_candidate(items[0])
    assert c.product_id == SCENES[0] and c.processing_baseline == "05.00"
    assert c.acquired.tzinfo is not None and c.tile_cloud_cover == pytest.approx(0.046619)


# -- plan --------------------------------------------------------------------------------------


def test_plan_estimates_window_bytes(
    adapter: EarthSearchSentinel2Adapter, items: list[dict[str, Any]]
) -> None:
    plan = adapter.plan(_request(window_bounds=list(WINDOW)))
    assert len(plan.products) == 3 and plan.refusals == []
    per_scene = 256 * 256 * 2 + 128 * 128 * 2 + 128 * 128
    for product, item in zip(plan.products, items, strict=True):
        assert product.estimated_bytes == per_scene + len(json.dumps(item))
    assert plan.estimated_bytes == sum(p.estimated_bytes or 0 for p in plan.products)
    assert "native resolution" in plan.estimate_basis and "uncompressed" in plan.estimate_basis
    bbox_plan = adapter.plan(_request())
    assert (
        bbox_plan.estimated_bytes is not None and bbox_plan.estimated_bytes > plan.estimated_bytes
    )


def test_plan_without_epsg_reports_unknown(items: list[dict[str, Any]], tmp_path: Path) -> None:
    stripped = copy.deepcopy(items[:1])
    del stripped[0]["properties"]["proj:epsg"]
    adapter = EarthSearchSentinel2Adapter(FakeStac(stripped), repo_root=tmp_path, git_sha=None)
    plan = adapter.plan(_request())
    assert plan.estimated_bytes is None
    assert any("size unknown" in w for w in plan.warnings)


def test_plan_with_no_scenes_warns(tmp_path: Path) -> None:
    adapter = EarthSearchSentinel2Adapter(FakeStac([]), repo_root=tmp_path, git_sha=None)
    plan = adapter.plan(_request())
    assert plan.products == [] and not plan.fetchable
    assert any("no scenes" in w for w in plan.warnings)


# -- fetch (offline, via the committed crops) ----------------------------------------------------


def test_fetch_writes_bands_item_and_ledger(
    adapter: EarthSearchSentinel2Adapter, fixtures_dir: Path, tmp_path: Path
) -> None:
    request = _request(window_bounds=list(WINDOW), max_scenes=1)
    plan = adapter.plan(request)
    assert [p.product_id for p in plan.products] == ["S2B_44RLU_20210131_1_L2A"]
    ledger = JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")
    entries = adapter.fetch(
        plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: False
    )
    sid = "S2B_44RLU_20210131_1_L2A"
    base = f"data/raw/sentinel2_earthsearch/{AOI}/{sid}"
    assert [e.path for e in entries] == [
        f"{base}/item.json",
        f"{base}/SCL.tif",
        f"{base}/B03.tif",
        f"{base}/B11.tif",
    ]
    for e in entries:
        assert e.status is ManifestStatus.fetched and e.event_id == "chamoli-2021"
        assert e.path is not None
        assert sha256_and_size(tmp_path / e.path) == (e.sha256, e.size_bytes)
        assert e.time_start == e.time_end == datetime(2021, 1, 31, 5, 30, 31, 938000, tzinfo=UTC)
        assert e.licence_source_url and "Sentinel_Data_Legal_Notice" in e.licence_source_url
    item_entry, scl_entry, b03_entry, b11_entry = entries
    assert item_entry.params["kind"] == "stac_item"
    written_item = json.loads((tmp_path / str(item_entry.path)).read_text("utf-8"))
    assert written_item["id"] == sid
    fixture_dir = fixtures_dir / "sentinel2" / AOI / sid
    assert written_item == json.loads((fixture_dir / "item.json").read_text("utf-8"))
    for entry, stem in ((scl_entry, "SCL"), (b03_entry, "B03"), (b11_entry, "B11")):
        assert entry.params["band"] == stem and entry.params["window_bounds"] == list(WINDOW)
        assert entry.url and entry.url.endswith(f"/{sid}/{stem}.tif")
        with (
            rasterio.open(tmp_path / str(entry.path)) as out,
            rasterio.open(fixture_dir / f"{stem}.tif") as ref,
        ):
            assert out.crs == ref.crs and out.transform == ref.transform
            assert np.array_equal(out.read(1), ref.read(1))
    with rasterio.open(fixture_dir / "SCL.tif") as ref:
        expected_fraction = cloud_fraction(ref.read(1))
    assert scl_entry.params["aoi_cloud_fraction"] == expected_fraction
    assert b03_entry.params["aoi_cloud_fraction"] == expected_fraction
    assert scl_entry.params["scl_histogram"]["snow_or_ice"] > 0
    assert scl_entry.params["window_shape"] == [128, 128]
    assert b03_entry.params["window_shape"] == [256, 256]


def test_aoi_cloud_fraction_matches_candidate_table(
    adapter: EarthSearchSentinel2Adapter, fixtures_dir: Path
) -> None:
    table = json.loads((fixtures_dir / "sentinel2" / AOI / "candidates.json").read_text("utf-8"))
    by_id = {row["product_id"]: row for row in table["candidates"]}
    assert table["aoi_window_bounds"] == list(WINDOW)
    for product in adapter.search(_request()):
        fraction = adapter.aoi_cloud_fraction(product, BBOX, window_bounds=WINDOW)
        assert fraction == by_id[product.product_id]["aoi_cloud_shadow_snow_fraction"]
        assert by_id[product.product_id]["selected_role"] in ("pre", "post")


def test_fetch_without_scl_asset_refuses(
    items: list[dict[str, Any]], fixtures_dir: Path, tmp_path: Path
) -> None:
    broken = copy.deepcopy(items[:1])
    del broken[0]["assets"]["scl"]
    adapter = EarthSearchSentinel2Adapter(
        FakeStac(broken),
        raster_opener=local_opener(fixtures_dir, items),
        repo_root=tmp_path,
        git_sha=None,
    )
    plan = adapter.plan(_request(window_bounds=list(WINDOW)))
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(ValueError, match="no SCL asset"):
        adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: False)
    assert [e.status for e in ledger.entries()] == [ManifestStatus.failed]
