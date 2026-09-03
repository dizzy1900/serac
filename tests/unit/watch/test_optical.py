"""The orientation-correlation tracker, on fictional images with shifts we imposed ourselves."""

from __future__ import annotations

import numpy as np
import pytest

from serac.models.watch.optical import (
    NOT_AUTORIFT,
    ChipMatch,
    NoiseFloor,
    aggregate_to_units,
    match_chip,
    measure_noise_floor,
    orientation_image,
    track_pair,
)

RNG = np.random.default_rng(4242)


def _texture(shape: tuple[int, int] = (128, 128)) -> np.ndarray:
    """Smooth random texture: correlatable, but with no preferred direction."""
    from scipy import ndimage

    return ndimage.gaussian_filter(RNG.normal(0.0, 1.0, size=shape), sigma=2.0)


def _shift(image: np.ndarray, dy: int, dx: int) -> np.ndarray:
    return np.roll(np.roll(image, dy, axis=0), dx, axis=1)


def test_orientation_image_is_unit_magnitude_where_there_is_a_gradient() -> None:
    image = _texture((32, 32))
    orientation = orientation_image(image)
    magnitude = np.abs(orientation)
    assert np.allclose(magnitude[magnitude > 0], 1.0)


def test_orientation_image_of_a_constant_field_is_all_zero() -> None:
    assert not np.any(orientation_image(np.ones((16, 16))))


def test_orientation_is_invariant_to_a_brightness_scaling_and_offset() -> None:
    """The reason for using orientation at all: a sunnier scene must still correlate."""
    image = _texture((64, 64))
    brighter = 3.7 * image + 120.0
    assert np.allclose(orientation_image(image), orientation_image(brighter), atol=1e-9)


@pytest.mark.parametrize(("dy", "dx"), [(0, 0), (2, 3), (-4, 1), (5, -5)])
def test_match_chip_recovers_an_integer_shift(dy: int, dx: int) -> None:
    reference = _texture((64, 64))
    secondary = _shift(reference, dy, dx)
    found_dx, found_dy, quality = match_chip(reference, secondary)
    assert found_dx == pytest.approx(dx, abs=0.35)
    assert found_dy == pytest.approx(dy, abs=0.35)
    assert quality > 0


def test_match_chip_reports_zero_displacement_for_identical_chips() -> None:
    reference = _texture((64, 64))
    dx, dy, quality = match_chip(reference, reference.copy())
    assert (dx, dy) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert quality > 0


def test_match_chip_on_featureless_chips_returns_zero_and_no_quality() -> None:
    flat = np.ones((32, 32))
    assert match_chip(flat, flat) == (0.0, 0.0, 0.0)


def test_match_chip_rejects_mismatched_or_tiny_chips() -> None:
    with pytest.raises(ValueError, match="same shape"):
        match_chip(np.zeros((8, 8)), np.zeros((9, 9)))
    with pytest.raises(ValueError, match="at least 4x4"):
        match_chip(np.zeros((3, 3)), np.zeros((3, 3)))


def test_track_pair_finds_the_same_shift_in_every_chip() -> None:
    reference = _texture((160, 160))
    secondary = _shift(reference, 3, -2)
    matches = track_pair(reference, secondary, chip_px=32, step_px=32, max_shift_px=8)
    assert len(matches) == 25
    good = [m for m in matches if m.quality > 0.1]
    assert len(good) >= 20
    assert np.median([m.dx_px for m in good]) == pytest.approx(-2, abs=0.5)
    assert np.median([m.dy_px for m in good]) == pytest.approx(3, abs=0.5)


def test_track_pair_rejects_mismatched_scenes() -> None:
    with pytest.raises(ValueError, match="share a shape"):
        track_pair(np.zeros((32, 32)), np.zeros((64, 64)))


# -- noise floor ---------------------------------------------------------------------------


def test_noise_floor_is_measured_on_stable_ground_only() -> None:
    stable = np.zeros((100, 100), dtype=bool)
    stable[:50, :] = True
    matches = [ChipMatch(row=10, col=c, dx_px=0.1, dy_px=0.0, quality=1.0) for c in range(10)]
    matches += [ChipMatch(row=80, col=c, dx_px=9.0, dy_px=0.0, quality=1.0) for c in range(10)]
    floor = measure_noise_floor(matches, stable, pixel_m=10.0)
    assert floor.n_stable_chips == 10
    assert floor.median_abs_displacement_m == pytest.approx(1.0)


def test_noise_floor_is_nan_when_there_is_no_stable_ground_rather_than_zero() -> None:
    floor = measure_noise_floor(
        [ChipMatch(row=1, col=1, dx_px=1.0, dy_px=1.0, quality=1.0)],
        np.zeros((10, 10), dtype=bool),
        pixel_m=10.0,
    )
    assert floor.n_stable_chips == 0
    assert np.isnan(floor.median_abs_displacement_m)
    assert not floor.is_significant(1e6), "nothing is significant against an unmeasured floor"


def test_displacement_below_the_floor_is_reported_as_not_significant() -> None:
    labels = np.zeros((40, 40), dtype=np.int32)
    labels[:20, :] = 1
    labels[20:, :] = 2
    floor = NoiseFloor(median_abs_displacement_m=5.0, p95_abs_displacement_m=8.0, n_stable_chips=50)
    matches = [ChipMatch(row=5, col=c, dx_px=0.2, dy_px=0.0, quality=1.0) for c in range(6)]
    matches += [ChipMatch(row=30, col=c, dx_px=2.0, dy_px=0.0, quality=1.0) for c in range(6)]
    units = aggregate_to_units(matches, labels, {1: "su-1", 2: "su-2"}, 10.0, floor)
    assert units["su-1"]["displacement_m"] == pytest.approx(2.0)
    assert units["su-1"]["significant"] is False
    assert "noise floor" in units["su-1"]["reason"]
    assert units["su-2"]["displacement_m"] == pytest.approx(20.0)
    assert units["su-2"]["significant"] is True


def test_a_unit_with_too_few_good_chips_gets_no_displacement() -> None:
    labels = np.ones((20, 20), dtype=np.int32)
    floor = NoiseFloor(1.0, 2.0, 10)
    matches = [ChipMatch(row=5, col=5, dx_px=1.0, dy_px=0.0, quality=1.0)]
    units = aggregate_to_units(matches, labels, {1: "su-1"}, 10.0, floor)
    assert units["su-1"]["displacement_m"] is None
    assert units["su-1"]["significant"] is False


def test_low_quality_chips_are_discarded_before_aggregation() -> None:
    labels = np.ones((20, 20), dtype=np.int32)
    floor = NoiseFloor(0.5, 1.0, 10)
    matches = [ChipMatch(row=r, col=5, dx_px=50.0, dy_px=0.0, quality=0.001) for r in range(10)]
    units = aggregate_to_units(matches, labels, {1: "su-1"}, 10.0, floor)
    assert units["su-1"]["displacement_m"] is None


def test_the_module_says_it_is_not_autorift() -> None:
    assert "NOT autoRIFT" in NOT_AUTORIFT
    assert "ITS_LIVE" in NOT_AUTORIFT


def test_a_peak_on_the_search_boundary_is_failed_not_reported() -> None:
    """A displacement at the search radius cannot be told from one beyond it, so it fails.

    This is also what protects against a decorrelated chip, whose "peak" is just the largest
    value of a flat noise field and piles up at the window boundary.
    """
    reference = _texture((64, 64))
    at_search_radius = _shift(reference, 0, 4)
    assert match_chip(reference, at_search_radius, max_shift_px=4) == (0.0, 0.0, 0.0)


def test_a_pure_noise_pair_is_failed_rather_than_given_a_displacement() -> None:
    a, b = _texture((64, 64)), _texture((64, 64))
    dx, dy, quality = match_chip(a, b, max_shift_px=2)
    if quality > 0:
        # Not on the boundary: then it must at least be a small displacement, not the radius.
        assert max(abs(dx), abs(dy)) < 2.0
    else:
        assert (dx, dy) == (0.0, 0.0)


def test_a_shift_inside_the_search_radius_still_reports() -> None:
    reference = _texture((64, 64))
    dx, dy, quality = match_chip(reference, _shift(reference, 0, 2), max_shift_px=4)
    assert dx == pytest.approx(2, abs=0.35)
    assert dy == pytest.approx(0, abs=0.35)
    assert quality > 0


def test_a_heavy_tailed_stable_sample_is_flagged() -> None:
    assert NoiseFloor(
        median_abs_displacement_m=3.0, p95_abs_displacement_m=80.0, n_stable_chips=500
    ).heavy_tailed
    assert not NoiseFloor(
        median_abs_displacement_m=3.0, p95_abs_displacement_m=9.0, n_stable_chips=500
    ).heavy_tailed


def test_an_unmeasurable_floor_counts_as_heavy_tailed() -> None:
    assert NoiseFloor(float("nan"), float("nan"), 0).heavy_tailed
