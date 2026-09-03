"""Sign conventions, checked against answers worked out by hand rather than by the code."""

from __future__ import annotations

import math

import numpy as np
import pytest

from serac.models.watch.geometry import (
    apparent_dip_deg,
    downslope_unit_vector,
    heading_from_footprint,
    layover_shadow_masks,
    local_incidence_deg,
    look_azimuth_deg,
    los_sensitivity,
    los_unit_vector,
    slope_aspect,
    surface_normal,
)


def test_ascending_los_points_west_descending_points_east() -> None:
    """A right-looking ascending pass sits west of its target, a descending pass east."""
    asc_e, asc_n, asc_u = los_unit_vector(35.0, 350.0)
    des_e, des_n, des_u = los_unit_vector(35.0, 190.0)
    assert float(asc_e) < -0.5
    assert float(des_e) > 0.5
    assert float(asc_u) > 0 and float(des_u) > 0
    # Both have a small southward component at these headings.
    assert float(asc_n) < 0 and float(des_n) < 0


def test_los_unit_vector_is_a_unit_vector() -> None:
    e, n, u = los_unit_vector(np.array([20.0, 35.0, 45.0]), np.array([350.0, 190.0, 12.0]))
    assert np.allclose(np.sqrt(e**2 + n**2 + u**2), 1.0)


def test_look_azimuth_is_heading_plus_ninety() -> None:
    assert float(look_azimuth_deg(350.0)) == pytest.approx(80.0)
    assert float(look_azimuth_deg(190.0)) == pytest.approx(280.0)


def test_downslope_vector_of_a_north_facing_slope_points_north_and_down() -> None:
    e, n, u = downslope_unit_vector(30.0, 0.0)
    assert float(e) == pytest.approx(0.0, abs=1e-12)
    assert float(n) == pytest.approx(math.cos(math.radians(30.0)))
    assert float(u) == pytest.approx(-math.sin(math.radians(30.0)))


def test_normal_of_a_north_facing_slope_tilts_north() -> None:
    e, n, u = surface_normal(30.0, 0.0)
    assert float(n) > 0
    assert float(e) == pytest.approx(0.0, abs=1e-12)
    assert float(u) == pytest.approx(math.cos(math.radians(30.0)))


def test_flat_ground_normal_is_up_and_local_incidence_equals_incidence() -> None:
    assert float(local_incidence_deg(0.0, 0.0, 37.0, 350.0)) == pytest.approx(37.0)


def test_ascending_pass_sees_east_facing_slopes_and_is_nearly_blind_to_west_facing() -> None:
    """The classic asymmetry, worked through by hand for a 40 deg slope at 35 deg incidence.

    An ascending pass (heading 350 deg) has LOS ``u = (-0.565, -0.100, 0.819)``.

    * East-facing (aspect 90): ``d = (0.766, 0, -0.643)``, ``d.u = -0.433 - 0.527 = -0.960``.
      Moving downslope goes away from the satellite horizontally *and* vertically, so the two
      contributions add and the track sees almost all of the motion.
    * West-facing (aspect 270): ``d = (-0.766, 0, -0.643)``, ``d.u = 0.433 - 0.527 = -0.094``.
      The horizontal approach almost exactly cancels the vertical recession, so the track is
      nearly blind to it however fast the slope moves.

    This cancellation, not coherence, is why a single track is not enough to watch a whole
    valley, and it is the first thing the selection rule weighs.
    """
    heading, incidence, slope = 350.0, 35.0, 40.0
    east_facing = float(los_sensitivity(slope, 90.0, incidence, heading))
    north_facing = float(los_sensitivity(slope, 0.0, incidence, heading))
    west_facing = float(los_sensitivity(slope, 270.0, incidence, heading))
    assert east_facing == pytest.approx(0.960, abs=0.005)
    assert north_facing == pytest.approx(0.603, abs=0.005)
    assert west_facing == pytest.approx(0.094, abs=0.005)
    assert east_facing > north_facing > west_facing


def test_sensitivity_is_bounded() -> None:
    aspects = np.arange(0.0, 360.0, 5.0)
    values = los_sensitivity(np.full_like(aspects, 45.0), aspects, 35.0, 350.0)
    assert values.min() >= 0.0
    assert values.max() <= 1.0


def test_apparent_dip_is_the_full_slope_along_the_aspect_and_zero_across_it() -> None:
    assert float(apparent_dip_deg(30.0, 90.0, 90.0)) == pytest.approx(30.0)
    assert float(apparent_dip_deg(30.0, 90.0, 270.0)) == pytest.approx(-30.0)
    assert float(apparent_dip_deg(30.0, 90.0, 0.0)) == pytest.approx(0.0, abs=1e-9)


def test_layover_when_the_slope_facing_the_sensor_is_steeper_than_the_incidence() -> None:
    """Look azimuth 80 deg: a slope descending towards 80 deg faces the sensor."""
    slope = np.array([[20.0, 50.0]])
    aspect = np.array([[80.0, 80.0]])
    layover, shadow = layover_shadow_masks(slope, aspect, 35.0, 350.0)
    assert layover.tolist() == [[False, True]]
    assert shadow.tolist() == [[False, False]]


def test_shadow_when_the_back_slope_is_steeper_than_ninety_minus_incidence() -> None:
    slope = np.array([[40.0, 70.0]])
    aspect = np.array([[260.0, 260.0]])
    layover, shadow = layover_shadow_masks(slope, aspect, 35.0, 350.0)
    assert layover.tolist() == [[False, False]]
    assert shadow.tolist() == [[False, True]]


def test_slope_aspect_on_a_plane_tilted_to_the_south() -> None:
    """z decreases going south, so aspect is 180 deg and slope is atan(gradient)."""
    rows, cols = 5, 5
    # Row 0 is north; elevation falls towards the south (increasing row index).
    dem = np.tile(np.arange(rows, dtype=np.float64)[::-1][:, None], (1, cols)) * 30.0
    slope, aspect = slope_aspect(dem, 30.0, 30.0)
    assert slope[2, 2] == pytest.approx(45.0)
    assert aspect[2, 2] == pytest.approx(180.0)


def test_slope_aspect_on_a_plane_tilted_to_the_east() -> None:
    dem = np.tile(np.arange(5, dtype=np.float64)[None, ::-1], (5, 1)) * 30.0
    slope, aspect = slope_aspect(dem, 30.0, 30.0)
    assert slope[2, 2] == pytest.approx(45.0)
    assert aspect[2, 2] == pytest.approx(90.0)


def test_slope_aspect_of_flat_ground_is_zero() -> None:
    slope, aspect = slope_aspect(np.zeros((4, 4)), 30.0, 30.0)
    assert np.allclose(slope, 0.0)
    assert np.allclose(aspect, 0.0)


def test_slope_aspect_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="positive metres"):
        slope_aspect(np.zeros((4, 4)), 0.0, 30.0)
    with pytest.raises(ValueError, match="at least 3x3"):
        slope_aspect(np.zeros((2, 2)), 30.0, 30.0)


def test_heading_from_a_real_burst_footprint() -> None:
    """The footprint of `S1_275112_IW3_20200608T124746` (ASF, path 129 ascending).

    Long axis ~80 km east-west (range), short axis ~22 km (azimuth). By hand the short edge
    runs from (79.179361, 30.533362) to (79.139137, 30.729044): dE = -0.0402 deg x cos(30.6)
    = -0.0346, dN = 0.1957, so the azimuth is atan2(-0.0346, 0.1957) = -10.0 deg, i.e. 350 deg.
    """
    footprint = [
        (79.179361, 30.533362),
        (79.599491, 30.59436),
        (80.004081, 30.651662),
        (79.963771, 30.856453),
        (79.559221, 30.794598),
        (79.139137, 30.729044),
        (79.179361, 30.533362),
    ]
    heading = heading_from_footprint(footprint, "ASCENDING")
    assert heading == pytest.approx(350.0, abs=1.5)


def test_heading_flips_for_a_descending_pass_with_the_same_geometry() -> None:
    footprint = [
        (79.179361, 30.533362),
        (80.004081, 30.651662),
        (79.963771, 30.856453),
        (79.139137, 30.729044),
    ]
    asc = heading_from_footprint(footprint, "ASCENDING")
    des = heading_from_footprint(footprint, "DESCENDING")
    assert abs(((asc - des) % 360.0) - 180.0) < 1e-6


def test_heading_rejects_a_degenerate_footprint() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        heading_from_footprint([(0.0, 0.0), (1.0, 1.0)], "ASCENDING")
