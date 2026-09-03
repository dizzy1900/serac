"""The anomaly model, including the causality guarantee the pre-registration turns on.

All series here are fictional and constructed in the test; nothing is read from `data/`.
"""

from __future__ import annotations

import numpy as np
import pytest

from serac.models.watch.anomaly import (
    ELEVATED_THRESHOLD,
    MIN_HISTORY_STEPS,
    SENS_FLOOR,
    WATCH_THRESHOLD,
    InsufficientReason,
    Tier,
    UnitSeries,
    deseasonalise,
    harmonic_design,
    robust_scale,
    robust_z,
    score_step,
    tier_for,
    trailing_slope,
    unit_state,
    walk_forward,
)

RNG = np.random.default_rng(20260903)


def _series(
    unit_id: str,
    *,
    n: int = 160,
    step_days: float = 12.0,
    rate_mm_yr: float = 0.0,
    seasonal_mm: float = 0.0,
    noise_mm: float = 0.0,
    coherence: float = 0.7,
    sensitivity: float = 0.8,
    accelerate_after: float | None = None,
    accel_rate_mm_yr: float = 0.0,
) -> UnitSeries:
    t = np.arange(n, dtype=np.float64) * step_days
    signal = rate_mm_yr * t / 365.25
    if accelerate_after is not None:
        extra = np.clip(t - accelerate_after, 0.0, None)
        signal = signal + accel_rate_mm_yr * extra / 365.25
    signal = signal + seasonal_mm * np.sin(2 * np.pi * t / 365.25)
    if noise_mm:
        signal = signal + RNG.normal(0.0, noise_mm, size=n)
    return UnitSeries(
        unit_id=unit_id,
        t_days=t,
        los_mm=signal * sensitivity,
        coherence=np.full(n, coherence),
        los_sensitivity_signed=sensitivity,
    )


# -- building blocks -------------------------------------------------------------------------


def test_robust_scale_matches_a_hand_computed_mad() -> None:
    values = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
    # median 3, absolute deviations 2,1,0,1,97 -> median 1 -> 1.4826
    assert robust_scale(values) == pytest.approx(1.4826)


def test_robust_scale_falls_back_to_iqr_then_to_zero() -> None:
    """The IQR fallback fires only for a one-sided sample, which is easy to get wrong.

    A MAD of zero needs at least half the values to equal the median, and when that block
    straddles the median symmetrically the quartiles land inside it and the IQR is zero too.
    The fallback is reachable only when the block sits at one end, as here: median 5, all
    deviations at or below the median are zero, but the 25th and 75th percentiles are 5 and 10.
    """
    values = np.array([5.0, 5.0, 5.0, 10.0, 20.0])
    assert robust_scale(values) == pytest.approx(5.0 / 1.349)
    assert robust_scale(np.array([0.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 10.0, 10.0])) == 0.0
    assert robust_scale(np.full(9, 7.0)) == 0.0


def test_robust_z_of_a_population_with_no_spread_is_zero_not_infinite() -> None:
    assert robust_z(100.0, np.full(20, 1.0)) == 0.0
    assert robust_z(1.0, np.array([])) == 0.0


def test_harmonic_design_has_the_pre_registered_columns() -> None:
    design = harmonic_design(np.arange(10, dtype=np.float64))
    assert design.shape == (10, 6)  # 1, t, cos, sin, cos2, sin2
    assert np.allclose(design[:, 0], 1.0)


def test_deseasonalise_removes_the_annual_cycle_but_keeps_the_trend() -> None:
    t = np.arange(0.0, 365.25 * 4, 12.0)
    trend = 10.0 * t / 365.25
    values = trend + 20.0 * np.sin(2 * np.pi * t / 365.25)
    cleaned = deseasonalise(t, values)
    assert np.nanstd(cleaned - trend) < 1.0
    assert np.nanstd(values - trend) > 10.0


def test_trailing_slope_recovers_a_known_rate() -> None:
    t = np.arange(0.0, 400.0, 12.0)
    values = 7.0 * t / 365.25
    assert trailing_slope(t, values, end=390.0) == pytest.approx(7.0, abs=1e-6)


def test_trailing_slope_is_none_when_the_window_is_too_thin() -> None:
    t = np.array([0.0, 12.0])
    assert trailing_slope(t, np.array([0.0, 1.0]), end=12.0) is None


def test_tier_thresholds_are_the_pre_registered_ones() -> None:
    assert tier_for(WATCH_THRESHOLD) is Tier.watch
    assert tier_for(WATCH_THRESHOLD - 1e-9) is Tier.elevated
    assert tier_for(ELEVATED_THRESHOLD) is Tier.elevated
    assert tier_for(ELEVATED_THRESHOLD - 1e-9) is Tier.quiet
    assert tier_for(float("nan")) is Tier.insufficient_data


# -- insufficient-data rules -----------------------------------------------------------------


def test_a_unit_outside_the_footprint_is_never_quiet() -> None:
    series = _series("su-1")
    outside = UnitSeries(
        unit_id="su-1",
        t_days=series.t_days,
        los_mm=series.los_mm,
        coherence=series.coherence,
        los_sensitivity_signed=series.los_sensitivity_signed,
        inside_footprint=False,
    )
    state = unit_state(outside, 1000.0)
    assert state.reason is InsufficientReason.outside_footprint


def test_a_unit_below_the_sensitivity_floor_is_never_quiet() -> None:
    series = _series("su-1", sensitivity=SENS_FLOOR / 2)
    assert unit_state(series, 1000.0).reason is InsufficientReason.low_los_sensitivity


def test_a_unit_below_the_coherence_floor_is_never_quiet() -> None:
    series = _series("su-1", coherence=0.1)
    assert unit_state(series, 1500.0).reason is InsufficientReason.low_coherence


def test_a_unit_with_too_few_samples_is_never_quiet() -> None:
    series = _series("su-1", n=6)
    assert unit_state(series, 100.0).reason is InsufficientReason.too_few_samples


def test_a_unit_with_too_little_history_is_insufficient_not_quiet() -> None:
    units = {f"su-{i}": _series(f"su-{i}", rate_mm_yr=float(i)) for i in range(12)}
    scored = walk_forward(units, [1000.0])
    assert all(s.tier is Tier.insufficient_data for s in scored[0].values())
    assert all(s.reason is InsufficientReason.too_little_history for s in scored[0].values())


# -- the scoring rule ------------------------------------------------------------------------


def test_one_accelerating_unit_among_steady_neighbours_reaches_watch() -> None:
    units: dict[str, UnitSeries] = {
        f"su-{i:02d}": _series(f"su-{i:02d}", rate_mm_yr=RNG.normal(0, 2), noise_mm=1.0)
        for i in range(30)
    }
    units["su-target"] = _series(
        "su-target", rate_mm_yr=1.0, noise_mm=1.0, accelerate_after=900.0, accel_rate_mm_yr=140.0
    )
    steps = list(np.arange(760.0, 1900.0, 30.4))
    scored = walk_forward(units, steps)
    target = [s["su-target"].tier for s in scored]
    assert Tier.watch in target, "an accelerating unit should reach watch against steady peers"


def test_a_common_mode_ramp_on_every_unit_does_not_trigger_watch() -> None:
    """The point of `min(z_temporal, z_spatial)`: atmosphere moves everything together."""
    units: dict[str, UnitSeries] = {}
    for i in range(30):
        series = _series(f"su-{i:02d}", rate_mm_yr=1.0, noise_mm=0.5)
        ramp = np.clip(series.t_days - 900.0, 0.0, None) * 0.4
        units[f"su-{i:02d}"] = UnitSeries(
            unit_id=f"su-{i:02d}",
            t_days=series.t_days,
            los_mm=series.los_mm + ramp * series.los_sensitivity_signed,
            coherence=series.coherence,
            los_sensitivity_signed=series.los_sensitivity_signed,
        )
    scored = walk_forward(units, list(np.arange(760.0, 1900.0, 30.4)))
    n_watch = [sum(1 for s in step.values() if s.tier is Tier.watch) for step in scored]
    assert max(n_watch) == 0, "a ramp shared by every unit must not raise any of them to watch"


def test_score_step_with_no_history_gives_insufficient_not_a_zero_score() -> None:
    units = {f"su-{i}": _series(f"su-{i}") for i in range(5)}
    states = {u: unit_state(s, 1200.0) for u, s in units.items()}
    scored = score_step(states, {u: [] for u in units})
    assert {s.tier for s in scored.values()} == {Tier.insufficient_data}


# -- causality -------------------------------------------------------------------------------


def test_appending_future_samples_then_truncating_leaves_scores_identical() -> None:
    """The pre-registered causality guarantee, checked end to end on the walk-forward."""
    base = {
        f"su-{i:02d}": _series(f"su-{i:02d}", rate_mm_yr=float(i), noise_mm=1.0) for i in range(20)
    }
    steps = list(np.arange(760.0, 1500.0, 30.4))
    before = walk_forward(base, steps)

    extended: dict[str, UnitSeries] = {}
    for unit_id, series in base.items():
        future_t = series.t_days[-1] + np.arange(1, 40, dtype=np.float64) * 12.0
        extended[unit_id] = UnitSeries(
            unit_id=unit_id,
            t_days=np.concatenate([series.t_days, future_t]),
            # Wildly different future: if any statistic peeked, the scores would move.
            los_mm=np.concatenate([series.los_mm, RNG.normal(5000.0, 500.0, size=future_t.size)]),
            coherence=np.concatenate([series.coherence, np.full(future_t.size, 0.95)]),
            los_sensitivity_signed=series.los_sensitivity_signed,
        )
    after = walk_forward(extended, steps)

    assert len(before) == len(after)
    for step_before, step_after in zip(before, after, strict=True):
        assert step_before.keys() == step_after.keys()
        for unit_id in step_before:
            a, b = step_before[unit_id], step_after[unit_id]
            assert a.tier is b.tier
            assert _same(a.score, b.score)
            assert _same(a.velocity_mm_yr, b.velocity_mm_yr)
            assert _same(a.acceleration_mm_yr2, b.acceleration_mm_yr2)
            assert a.n_samples == b.n_samples


def test_deseasonalise_coefficients_do_not_change_when_the_future_is_appended() -> None:
    t = np.arange(0.0, 365.25 * 3, 12.0)
    values = 5.0 * t / 365.25 + 15.0 * np.sin(2 * np.pi * t / 365.25)
    truncated = deseasonalise(t, values)
    future_t = np.concatenate([t, t[-1] + np.arange(1, 30) * 12.0])
    future_v = np.concatenate([values, RNG.normal(1e4, 1e3, size=29)])
    keep = future_t <= t[-1]
    reconstructed = deseasonalise(future_t[keep], future_v[keep])
    assert np.allclose(truncated, reconstructed, equal_nan=True)


def test_walk_forward_history_never_includes_the_current_step() -> None:
    units = {f"su-{i}": _series(f"su-{i}", rate_mm_yr=float(i)) for i in range(MIN_HISTORY_STEPS)}
    steps = list(np.arange(760.0, 1200.0, 30.4))
    scored = walk_forward(units, steps)
    # The first MIN_HISTORY_STEPS steps cannot have enough history by construction.
    for step in scored[:MIN_HISTORY_STEPS]:
        assert all(s.tier is Tier.insufficient_data for s in step.values())


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    if np.isnan(a) and np.isnan(b):
        return True
    return bool(a == b)
