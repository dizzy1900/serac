"""The MintPy config, the pre-registered reference-point rule, and the ROI_PAC conversion."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine

from serac.errors import SeracError
from serac.models.watch.mintpy_prep import raster_metadata, write_bil_pair
from serac.models.watch.mintpy_run import (
    REFERENCE_MAX_SLOPE_DEG,
    REFERENCE_MIN_TEMPORAL_COHERENCE,
    TROPO_CAVEAT,
    MintPyConfig,
    choose_reference_point,
)


def _config(**overrides: object) -> MintPyConfig:
    base = {
        "aoi_id": "test-aoi",
        "unw_glob": "/tmp/x/*.unw",
        "cor_glob": "/tmp/x/*.cor",
        "dem_glob": "/tmp/x/geom_dem/*.hgt",
        "inc_angle_glob": "/tmp/x/geom_inc/*.hgt",
    }
    base.update(overrides)
    return MintPyConfig(**base)  # type: ignore[arg-type]


# -- config ---------------------------------------------------------------------------------


def test_the_config_declares_roipac_and_the_height_correlation_correction() -> None:
    options = _config().as_options()
    assert options["mintpy.load.processor"] == "roipac"
    assert options["mintpy.troposphericDelay.method"] == "height_correlation"


def test_every_geometry_key_is_set_explicitly_so_a_stale_template_cannot_leak() -> None:
    """MintPy merges over the cfg left by a previous run, so omitted keys keep their old values."""
    options = _config().as_options()
    for key in (
        "mintpy.load.azAngleFile",
        "mintpy.load.waterMaskFile",
        "mintpy.load.metaFile",
        "mintpy.load.baselineDir",
    ):
        assert options[key] == "auto"


def test_the_reference_point_appears_only_once_it_is_known() -> None:
    assert "mintpy.reference.yx" not in _config().as_options()
    pinned = _config(reference_yx=(12, 34)).as_options()
    assert pinned["mintpy.reference.yx"] == "12, 34"


def test_the_config_digest_changes_with_the_reference_point() -> None:
    assert _config().digest() != _config(reference_yx=(1, 2)).digest()
    assert _config(reference_yx=(1, 2)).digest() == _config(reference_yx=(1, 2)).digest()


def test_the_rendered_config_carries_the_tropospheric_caveat() -> None:
    rendered = _config().render()
    assert TROPO_CAVEAT.split(".")[0] in rendered
    assert rendered.count("mintpy.load.processor = roipac") == 1


# -- reference point ------------------------------------------------------------------------


def _flat(shape: tuple[int, int] = (5, 5)) -> np.ndarray:
    return np.zeros(shape, dtype=np.float64)


def test_the_reference_point_is_the_most_coherent_eligible_pixel() -> None:
    coherence = np.full((4, 4), 0.9)
    coherence[2, 3] = 0.99
    point = choose_reference_point(coherence, _flat((4, 4)), np.zeros((4, 4), dtype=bool))
    assert (point.row, point.col) == (2, 3)
    assert point.temporal_coherence == pytest.approx(0.99)
    assert point.n_candidates == 16


def test_ties_break_on_the_lowest_row_then_the_lowest_column() -> None:
    coherence = np.full((3, 3), 0.9)
    point = choose_reference_point(coherence, _flat((3, 3)), np.zeros((3, 3), dtype=bool))
    assert (point.row, point.col) == (0, 0)


def test_steep_layover_and_shadow_pixels_are_never_chosen() -> None:
    coherence = np.full((3, 3), 0.9)
    coherence[0, 0] = 1.0  # best, but steep
    coherence[0, 1] = 0.99  # second best, but layover
    slope = _flat((3, 3))
    slope[0, 0] = REFERENCE_MAX_SLOPE_DEG + 1.0
    blocked = np.zeros((3, 3), dtype=bool)
    blocked[0, 1] = True
    point = choose_reference_point(coherence, slope, blocked)
    assert (point.row, point.col) not in {(0, 0), (0, 1)}
    assert point.n_candidates == 7


def test_the_rule_refuses_rather_than_relaxing_when_nothing_qualifies() -> None:
    coherence = np.full((3, 3), REFERENCE_MIN_TEMPORAL_COHERENCE - 0.01)
    with pytest.raises(SeracError, match="pre-registered"):
        choose_reference_point(coherence, _flat((3, 3)), np.zeros((3, 3), dtype=bool))


def test_non_finite_coherence_is_not_eligible() -> None:
    coherence = np.full((3, 3), 0.9)
    coherence[1, 1] = np.nan
    point = choose_reference_point(coherence, _flat((3, 3)), np.zeros((3, 3), dtype=bool))
    assert point.n_candidates == 8


def test_mismatched_input_shapes_are_rejected() -> None:
    with pytest.raises(ValueError, match="share a shape"):
        choose_reference_point(np.zeros((3, 3)), np.zeros((4, 4)), np.zeros((3, 3), dtype=bool))


# -- ROI_PAC conversion ---------------------------------------------------------------------


def test_bil_layout_puts_the_quantity_in_band_two(tmp_path: Path) -> None:
    """MintPy reads band 2 of a ROI_PAC `.unw`, so band 1 must be the amplitude."""
    amplitude = np.full((3, 4), 7.0, dtype=np.float32)
    value = np.arange(12, dtype=np.float32).reshape(3, 4)
    dest = write_bil_pair(amplitude, value, tmp_path / "x.unw")
    raw = np.fromfile(dest, dtype=np.float32).reshape(3, 2, 4)
    assert np.array_equal(raw[:, 0, :], amplitude)
    assert np.array_equal(raw[:, 1, :], value)


def test_bil_writer_rejects_mismatched_rasters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="same shape"):
        write_bil_pair(
            np.zeros((2, 2), dtype=np.float32),
            np.zeros((3, 3), dtype=np.float32),
            tmp_path / "x.unw",
        )


def test_raster_metadata_matches_the_gdal_vrt_keys(tmp_path: Path) -> None:
    """The rasterio stand-in for `read_gdal_vrt` must produce the same corner convention."""
    path = tmp_path / "t.tif"
    transform = Affine(80.0, 0.0, 358_480.0, 0.0, -80.0, 3_386_560.0)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "width": 5,
        "height": 4,
        "crs": CRS.from_epsg(32644),
        "transform": transform,
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.zeros((4, 5), dtype=np.float32), 1)
    meta = raster_metadata(path)
    assert int(meta["WIDTH"]) == 5
    assert int(meta["LENGTH"]) == 4
    assert float(meta["X_FIRST"]) == pytest.approx(358_480.0)
    assert float(meta["Y_FIRST"]) == pytest.approx(3_386_560.0)
    assert float(meta["X_STEP"]) == pytest.approx(80.0)
    assert float(meta["Y_STEP"]) == pytest.approx(-80.0)
    assert meta["UTM_ZONE"] == "44N"
    assert meta["X_UNIT"] == "meters"


def test_incidence_conversion_from_hyp3_elevation_angle() -> None:
    """`lv_theta` is elevation above horizontal; MintPy wants incidence from vertical."""
    elevation_rad = np.radians(np.array([57.5, 56.2, 58.8], dtype=np.float32))
    incidence_deg = 90.0 - np.degrees(elevation_rad)
    assert incidence_deg == pytest.approx([32.5, 33.8, 31.2], abs=0.05)
