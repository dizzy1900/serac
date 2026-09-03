"""SCL cloud fraction and scene selection: synthetic arrays for edges, real crops for values."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import rasterio

from serac.adapters.eo.s2_cloud import (
    CLOUD_ONLY_CLASSES,
    CLOUD_SHADOW_SNOW_CLASSES,
    SCL_LEGEND,
    SceneCandidate,
    class_histogram,
    cloud_fraction,
    collapse_reprocessings,
    select_pre_post,
    select_scenes,
)

AOI = "chamoli-rishiganga"
T = datetime(2021, 2, 1, tzinfo=UTC)


def _cand(pid: str, days: float, **kw: object) -> SceneCandidate:
    return SceneCandidate(product_id=pid, acquired=T + timedelta(days=days), **kw)  # type: ignore[arg-type]


# -- cloud_fraction ----------------------------------------------------------------------------


def test_classes_are_the_documented_sets() -> None:
    assert frozenset({3, 8, 9, 10, 11}) == CLOUD_SHADOW_SNOW_CLASSES
    assert frozenset({3, 8, 9, 10}) == CLOUD_ONLY_CLASSES
    assert SCL_LEGEND[11] == "snow_or_ice" and SCL_LEGEND[0] == "no_data"


def test_cloud_fraction_synthetic() -> None:
    scl = np.array([[4, 4, 8, 9], [11, 3, 5, 6]], dtype=np.uint8)
    assert cloud_fraction(scl) == pytest.approx(4 / 8)
    assert cloud_fraction(scl, classes=CLOUD_ONLY_CLASSES) == pytest.approx(3 / 8)
    assert cloud_fraction(scl, classes=()) == 0.0
    assert cloud_fraction(np.full((4, 4), 4, dtype=np.uint8)) == 0.0
    assert cloud_fraction(np.full((4, 4), 9, dtype=np.uint8)) == 1.0


def test_cloud_fraction_ignores_nodata_and_is_none_when_all_nodata() -> None:
    scl = np.array([[0, 0, 0, 9]], dtype=np.uint8)
    assert cloud_fraction(scl) == 1.0  # one valid pixel, cloudy
    assert cloud_fraction(np.zeros((3, 3), dtype=np.uint8)) is None  # unknown is not clear
    assert cloud_fraction(np.array([[7, 7, 9]]), nodata=7) == 1.0


def test_class_histogram() -> None:
    hist = class_histogram(np.array([[0, 4, 4, 11, 12]], dtype=np.uint8))
    assert hist == {"no_data": 1, "vegetation": 2, "snow_or_ice": 1, "class_12": 1}


@pytest.mark.parametrize(
    "scene", ["S2A_44RLU_20210126_1_L2A", "S2B_44RLU_20210131_1_L2A", "S2B_44RLU_20210210_1_L2A"]
)
def test_cloud_fraction_on_committed_scl_matches_candidate_table(
    fixtures_dir: Path, scene: str
) -> None:
    table = json.loads((fixtures_dir / "sentinel2" / AOI / "candidates.json").read_text("utf-8"))
    row = next(r for r in table["candidates"] if r["product_id"] == scene)
    with rasterio.open(fixtures_dir / "sentinel2" / AOI / scene / "SCL.tif") as ds:
        scl = ds.read(1)
    assert scl.shape == (128, 128) and scl.dtype == np.uint8
    assert cloud_fraction(scl) == row["aoi_cloud_shadow_snow_fraction"]
    assert cloud_fraction(scl, classes=CLOUD_ONLY_CLASSES) == row["aoi_cloud_only_fraction"]
    assert class_histogram(scl) == row["scl_histogram"]
    assert (scl == 0).sum() == 0  # the fixture window is fully inside the tile


# -- candidates and selection ------------------------------------------------------------------


def test_ranking_fraction_prefers_aoi_then_tile_then_none() -> None:
    assert _cand("a", 0, aoi_cloud_fraction=0.2, tile_cloud_cover=90.0).ranking_fraction == 0.2
    assert _cand("b", 0, tile_cloud_cover=25.0).ranking_fraction == 0.25
    assert _cand("c", 0).ranking_fraction is None


def test_collapse_reprocessings_keeps_newest_baseline_per_instant() -> None:
    old = _cand("x_0", 0, processing_baseline="02.14", tile_cloud_cover=15.0)
    new = SceneCandidate(
        product_id="x_1",
        acquired=old.acquired - timedelta(milliseconds=1),
        processing_baseline="05.00",
        tile_cloud_cover=8.0,
    )
    other = _cand("y_0", 5, processing_baseline="02.14")
    unknown = _cand("z", 9)
    assert [c.product_id for c in collapse_reprocessings([old, new, other, unknown])] == [
        "x_1",
        "y_0",
        "z",
    ]


def test_select_scenes_orders_by_fraction_then_recency() -> None:
    cands = [
        _cand("clear_old", 0, aoi_cloud_fraction=0.1),
        _cand("clear_new", 3, aoi_cloud_fraction=0.1),
        _cand("cloudy", 1, aoi_cloud_fraction=0.9),
        _cand("tile_only", 2, tile_cloud_cover=50.0),
        _cand("unknown", 4),
    ]
    assert [c.product_id for c in select_scenes(cands, n=1)] == ["clear_new"]
    assert [c.product_id for c in select_scenes(cands, n=3)] == [
        "clear_old",
        "tile_only",
        "clear_new",
    ]  # chronological output order
    assert [c.product_id for c in select_scenes(cands, n=10, max_fraction=0.2)] == [
        "clear_old",
        "clear_new",
    ]
    assert "unknown" not in [c.product_id for c in select_scenes(cands, n=10)]
    assert select_scenes(cands, n=0) == []
    with pytest.raises(ValueError):
        select_scenes(cands, n=-1)


def test_select_scenes_window() -> None:
    cands = [_cand("a", 0, aoi_cloud_fraction=0.0), _cand("b", 10, aoi_cloud_fraction=0.0)]
    chosen = select_scenes(cands, n=5, window=(T + timedelta(days=5), T + timedelta(days=20)))
    assert [c.product_id for c in chosen] == ["b"]


def test_select_pre_post_splits_on_event_time() -> None:
    cands = [
        _cand("pre_a", -10, aoi_cloud_fraction=0.3),
        _cand("pre_b", -5, aoi_cloud_fraction=0.1),
        _cand("pre_c", -1, aoi_cloud_fraction=0.2),
        _cand("post_a", 3, aoi_cloud_fraction=0.6),
        _cand("post_b", 8, aoi_cloud_fraction=0.5),
    ]
    pre, post = select_pre_post(cands, event_time=T, n_pre=2, n_post=1)
    assert [c.product_id for c in pre] == ["pre_b", "pre_c"]
    assert [c.product_id for c in post] == ["post_b"]
    pre, post = select_pre_post(cands, event_time=T, n_pre=5, n_post=5, max_fraction=0.55)
    assert len(pre) == 3 and [c.product_id for c in post] == ["post_b"]
