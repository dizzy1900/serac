"""Slope-unit delineation on fictional DEMs whose correct segmentation is obvious by eye."""

from __future__ import annotations

import numpy as np
import pytest

from serac.domain.geo import GridSpec
from serac.models.watch.raster import GriddedDem
from serac.models.watch.slope_units import (
    METHOD_DESCRIPTION,
    MIN_SLOPE_DEG,
    _circular_mean_aspect,
    _dissolve_small,
    delineate,
    unit_table,
)


def _grid(width: int, height: int) -> GridSpec:
    return GridSpec(
        aoi_id="test-aoi",
        epsg=32644,
        resolution_m=30.0,
        x_min=300_000.0,
        y_min=3_000_000.0,
        x_max=300_000.0 + width * 30.0,
        y_max=3_000_000.0 + height * 30.0,
        width=width,
        height=height,
    )


def _dem(elevation: np.ndarray) -> GriddedDem:
    height, width = elevation.shape
    return GriddedDem(
        grid=_grid(width, height),
        elevation_m=elevation.astype(np.float64),
        source_path="tests/fictional.tif",
        source_sha256="0" * 64,
        resampling="bilinear",
    )


def _ridge(size: int = 60, gradient_m_per_px: float = 30.0) -> np.ndarray:
    """A north-south ridge: the west flank faces west, the east flank faces east."""
    columns = np.arange(size, dtype=np.float64)
    profile = gradient_m_per_px * (size / 2 - np.abs(columns - size / 2))
    return np.tile(profile, (size, 1)) + 3000.0


def test_circular_mean_averages_across_the_north_seam() -> None:
    """350 and 10 degrees average to about 0, not to 180 as an arithmetic mean would give.

    The window is edge-replicated, so individual pixels sit a few degrees either side of due
    north rather than exactly on it; what matters is that none of them lands on the opposite
    bearing, which is what a naive mean of the degree values produces.
    """
    aspect = np.tile(np.array([350.0, 10.0]), (5, 3))
    smoothed = _circular_mean_aspect(aspect, window=3)
    wrapped = np.mod(smoothed + 180.0, 360.0) - 180.0
    assert np.abs(wrapped).max() < 10.0, smoothed
    assert np.abs(np.abs(wrapped) - 180.0).min() > 100.0


def test_a_symmetric_ridge_splits_into_two_facing_families() -> None:
    delineation = delineate(_dem(_ridge()), min_area_m2=9000.0)
    rows = unit_table(delineation)
    assert len(rows) >= 2
    aspects = [r["aspect_deg"] for r in rows if r["n_pixels"] > 20]
    east = [a for a in aspects if 45 < a < 135]
    west = [a for a in aspects if 225 < a < 315]
    assert east and west, f"expected east- and west-facing units, got aspects {aspects}"


def test_flat_ground_produces_no_units_at_all() -> None:
    delineation = delineate(_dem(np.full((40, 40), 2000.0)))
    assert delineation.unit_ids == []
    assert unit_table(delineation) == []


def test_terrain_below_the_slope_threshold_is_excluded() -> None:
    """A 1 m/px gradient over 30 m pixels is ~1.9 degrees, well under the 15 degree floor."""
    gentle = _ridge(gradient_m_per_px=1.0)
    assert delineate(_dem(gentle)).unit_ids == []
    steep = _ridge(gradient_m_per_px=30.0)
    assert delineate(_dem(steep)).unit_ids != []
    assert MIN_SLOPE_DEG == 15.0


def test_delineation_is_deterministic_and_the_digest_tracks_the_labels() -> None:
    dem = _dem(_ridge())
    first = delineate(dem, min_area_m2=9000.0)
    second = delineate(dem, min_area_m2=9000.0)
    assert np.array_equal(first.labels, second.labels)
    assert first.digest() == second.digest()
    third = delineate(dem, min_area_m2=90_000.0)
    assert third.digest() != first.digest()


def test_the_digest_changes_when_the_source_dem_checksum_changes() -> None:
    elevation = _ridge()
    a = delineate(_dem(elevation), min_area_m2=9000.0)
    other = GriddedDem(
        grid=a.dem.grid,
        elevation_m=a.dem.elevation_m,
        source_path=a.dem.source_path,
        source_sha256="1" * 64,
        resampling="bilinear",
    )
    b = delineate(other, min_area_m2=9000.0)
    assert np.array_equal(a.labels, b.labels)
    assert a.digest() != b.digest()


def test_every_unit_is_at_least_the_minimum_area() -> None:
    delineation = delineate(_dem(_ridge(size=80)), min_area_m2=40_000.0)
    rows = unit_table(delineation)
    assert rows
    assert all(r["area_m2"] >= 40_000.0 for r in rows)


def test_small_components_are_merged_into_their_largest_neighbour() -> None:
    labels = np.array(
        [
            [1, 1, 1, 1],
            [1, 1, 1, 1],
            [1, 1, 2, 1],
            [1, 1, 1, 1],
        ],
        dtype=np.int32,
    )
    merged = _dissolve_small(labels, min_pixels=4)
    assert set(np.unique(merged)) == {1}


def test_a_small_component_with_no_neighbour_is_dropped_not_kept() -> None:
    labels = np.zeros((6, 6), dtype=np.int32)
    labels[2, 2] = 7
    merged = _dissolve_small(labels, min_pixels=4)
    assert not merged.any()


def test_unit_rows_carry_the_fields_the_contract_needs() -> None:
    rows = unit_table(delineate(_dem(_ridge()), min_area_m2=9000.0))
    row = rows[0]
    for key in (
        "unit_id",
        "unit_index",
        "area_m2",
        "mean_slope_deg",
        "aspect_deg",
        "elevation_min_m",
        "elevation_max_m",
        "geometry",
    ):
        assert key in row
    assert 0.0 <= row["aspect_deg"] < 360.0
    assert 0.0 <= row["mean_slope_deg"] <= 90.0
    assert row["elevation_min_m"] <= row["elevation_max_m"]
    assert row["geometry"].area == pytest.approx(row["area_m2"], rel=1e-6)


def test_the_method_is_labelled_as_not_r_slopeunits() -> None:
    assert "NOT r.slopeunits" in METHOD_DESCRIPTION


def test_delineate_rejects_an_all_nan_dem() -> None:
    with pytest.raises(ValueError, match="no finite elevations"):
        delineate(_dem(np.full((10, 10), np.nan)))
