"""The mass estimators and the trajectory they stand on.

The mass is the number most likely to be quoted out of context and least likely to be
checked, so the tests here are about the ways it could be wrong while looking right:

* a friction coefficient above `tan(theta)` describes a mass that cannot move, and sampling
  one inflates the mass by an order of magnitude on any shallow-runout event;
* the slide moves *opposite* the force it exerts, so a DEM profile drawn along the force
  azimuth points uphill;
* a point mass must be impossible to construct, and the union of two estimators must be a
  union rather than a quietly narrowed average.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from serac.domain.force_history import MassEstimate
from serac.models.lfh.config import MassConfig
from serac.models.lfh.mass import (
    ForceSummary,
    combine,
    dem_trajectory_estimate,
    effective_acceleration,
    seismic_impulse_estimate,
    summarise,
)
from serac.models.lfh.trajectory import (
    Trajectory,
    integrate,
    path_angle_from_force,
    runout_azimuth,
    slope_from_drop_and_runout,
    unit_mass_displacement,
)


def _pulse(n: int = 301, *, north: float = 1.0, east: float = 0.0, up: float = -0.45) -> np.ndarray:
    """An acceleration-then-deceleration force, zero at both ends, peaking at 1.4e11 N."""
    t = np.arange(n)
    shape = np.exp(-((t - n * 0.35) ** 2) / (2 * (n * 0.12) ** 2)) - 0.7 * np.exp(
        -((t - n * 0.62) ** 2) / (2 * (n * 0.15) ** 2)
    )
    shape[0] = shape[-1] = 0.0
    force = np.vstack([up * shape, north * shape, east * shape])
    return force * (1.4e11 / float(np.linalg.norm(force, axis=0).max()))


# --- trajectory -----------------------------------------------------------------------------


def test_the_mass_moves_opposite_the_force_it_exerts() -> None:
    """Newton's third law, which is the whole basis of a single-force inversion.

    A slide accelerating northward pushes the ground southward, so the inverted force points
    south while the displacement points north. Getting this backwards sends the DEM profile
    up the mountain instead of down it.
    """
    forces = _pulse(north=1.0, east=0.0)
    trajectory = integrate(forces, dt=1.0, mass_kg=1e11)
    force_bearing = summarise(forces, dt=1.0).azimuth_deg
    motion_bearing = runout_azimuth(forces, dt=1.0)

    assert force_bearing == pytest.approx(0.0, abs=1.0), "the force points north"
    assert motion_bearing == pytest.approx(180.0, abs=5.0), "the mass therefore moved south"
    separation = abs((motion_bearing - force_bearing + 180.0) % 360.0 - 180.0)
    assert separation > 150.0, f"force and motion must be near-opposite; got {separation:.0f} deg"
    assert trajectory.displacement_m[1, -1] < 0


def test_displacement_scales_inversely_with_mass() -> None:
    """Every displacement goes as 1/M, which is what leaves exactly one unknown for the DEM."""
    forces = _pulse()
    light = integrate(forces, dt=1.0, mass_kg=1e10)
    heavy = integrate(forces, dt=1.0, mass_kg=1e11)
    np.testing.assert_allclose(light.displacement_m, heavy.displacement_m * 10.0, rtol=1e-9)
    unit = unit_mass_displacement(forces, dt=1.0)
    np.testing.assert_allclose(unit, heavy.displacement_m[:, -1] * 1e11, rtol=1e-9)


def test_trajectory_summary_quantities_are_consistent() -> None:
    forces = _pulse()
    trajectory = integrate(forces, dt=1.0, mass_kg=1e11)
    assert trajectory.peak_speed_m_s > 0
    assert (
        trajectory.path_length_m
        >= math.hypot(trajectory.horizontal_runout_m, trajectory.drop_m) - 1e-6
    )
    assert 0.0 <= trajectory.path_angle_deg <= 90.0
    assert isinstance(trajectory, Trajectory)


def test_integrate_rejects_a_non_positive_mass() -> None:
    with pytest.raises(ValueError, match="mass must be positive"):
        integrate(_pulse(), dt=1.0, mass_kg=0.0)


def test_path_angle_from_force_reads_the_slope_off_the_waveform() -> None:
    """`theta = atan(|F_vertical| / |F_horizontal|)` at the peak of the horizontal force."""
    steep = _pulse(north=1.0, up=-1.0)
    shallow = _pulse(north=1.0, up=-0.2)
    assert path_angle_from_force(steep) == pytest.approx(45.0, abs=1.0)
    assert path_angle_from_force(shallow) < path_angle_from_force(steep)
    assert slope_from_drop_and_runout(850.0, 2950.0) == pytest.approx(
        math.degrees(math.atan2(850.0, 2950.0)), abs=1e-9
    )


# --- friction physics ------------------------------------------------------------------------


def test_effective_acceleration_is_positive_for_every_slope() -> None:
    """`a_eff = g sin(theta) (1 - phi)` cannot go negative, whatever the friction ratio."""
    for angle in (8.0, 16.1, 30.0, 55.0):
        for ratio in (0.2, 0.5, 0.8, 0.99):
            assert effective_acceleration(angle, ratio, 9.81) > 0


def test_an_absolute_friction_above_tan_theta_would_have_broken_bingham() -> None:
    """The bug this parameterisation removes, stated as arithmetic.

    Bingham Canyon has H/L = 850/2950, so tan(theta) = 0.29. An absolute Coulomb coefficient
    of 0.45 -- unremarkable in a table of rock friction values -- makes `sin(theta) - mu
    cos(theta)` negative, which a floor then turns into a near-zero acceleration and a mass an
    order of magnitude too large. Expressed as a fraction of tan(theta) the quantity stays
    physical for any ratio below one.
    """
    theta = math.degrees(math.atan2(850.0, 2950.0))
    radians = math.radians(theta)
    naive = 9.81 * (math.sin(radians) - 0.45 * math.cos(radians))
    assert naive < 0, "the old absolute range really did go unphysical at Bingham's slope"

    peak_force = 1.65e11
    assert peak_force / max(naive, 0.05) > 3e12, "and it produced an absurd mass"
    principled = peak_force / effective_acceleration(theta, 0.8, 9.81)
    assert 1e10 < principled < 1e12, "while the ratio form stays in the right decade"


# --- the estimators ---------------------------------------------------------------------------


def test_summarise_reads_peak_impulse_and_duration() -> None:
    forces = _pulse()
    summary = summarise(forces, dt=1.0)
    assert isinstance(summary, ForceSummary)
    assert summary.is_usable
    assert summary.peak_force_n == pytest.approx(1.4e11, rel=1e-6)
    assert summary.impulse_ns > 0
    assert 0 < summary.acceleration_time_s < summary.duration_s
    assert summary.onset_index < summary.peak_index


def test_summarise_survives_a_silent_force() -> None:
    summary = summarise(np.zeros((3, 51)), dt=1.0)
    assert not summary.is_usable
    assert summary.peak_force_n == 0.0


def test_both_estimators_return_strict_intervals() -> None:
    config = MassConfig()
    forces = _pulse()
    a = dem_trajectory_estimate(
        forces,
        dt=1.0,
        config=config,
        profile=None,
        published_drop_m=850.0,
        published_runout_m=2950.0,
        published_source="esec-bingham-1",
    )
    b = seismic_impulse_estimate(forces, dt=1.0, config=config)
    for estimate in (a, b):
        assert estimate.mass_kg_p05 < estimate.mass_kg_p50 < estimate.mass_kg_p95
        assert estimate.assumptions, "every estimator must name what it assumed"
        assert isinstance(estimate.as_estimate(), MassEstimate)
    assert a.method == "fmax_over_aeff"
    assert b.method == "impulse_over_velocity"


def test_a_published_geometry_is_labelled_assumed_not_dem() -> None:
    """`AEff.basis` must not claim `dem_trajectory` when no DEM was read."""
    a = dem_trajectory_estimate(
        _pulse(),
        dt=1.0,
        config=MassConfig(),
        profile=None,
        published_drop_m=850.0,
        published_runout_m=2950.0,
        published_source="esec-bingham-1",
    )
    assert a.a_eff.basis == "assumed_range"
    assert any("No DEM crop" in text for text in a.assumptions)


def test_the_degraded_fallback_says_it_is_degraded() -> None:
    """With no DEM and no published geometry the estimators stop being independent."""
    a = dem_trajectory_estimate(_pulse(), dt=1.0, config=MassConfig(), profile=None)
    assert a.a_eff.basis == "assumed_range"
    assert any(text.startswith("DEGRADED") for text in a.assumptions)
    assert any("no longer independent" in text for text in a.assumptions)


def test_the_seismic_estimator_uses_no_external_geometry() -> None:
    """Estimator B must produce the same answer whatever a DEM or catalogue would have said."""
    forces = _pulse()
    first = seismic_impulse_estimate(forces, dt=1.0, config=MassConfig())
    second = seismic_impulse_estimate(forces, dt=1.0, config=MassConfig())
    assert first.mass_kg_p50 == pytest.approx(second.mass_kg_p50)
    assert first.a_eff.slope_deg == pytest.approx(path_angle_from_force(forces), abs=1e-9)


def test_combine_takes_the_union_and_reports_the_ratio() -> None:
    """The published interval must not be narrower than either estimator's."""
    config = MassConfig()
    forces = _pulse()
    a = dem_trajectory_estimate(
        forces,
        dt=1.0,
        config=config,
        profile=None,
        published_drop_m=850.0,
        published_runout_m=2950.0,
    )
    b = seismic_impulse_estimate(forces, dt=1.0, config=config)
    combined = combine(a, b)

    assert combined.method == "combined"
    assert combined.mass_kg_p05 == pytest.approx(min(a.mass_kg_p05, b.mass_kg_p05))
    assert combined.mass_kg_p95 == pytest.approx(max(a.mass_kg_p95, b.mass_kg_p95))
    assert combined.consistency_ratio == pytest.approx(a.mass_kg_p50 / b.mass_kg_p50)
    assert any("UNION" in text for text in combined.assumptions)
    assert any("NOT independent" in text for text in combined.assumptions)


def test_a_wide_disagreement_is_reported_not_reconciled() -> None:
    config = MassConfig()
    a = dem_trajectory_estimate(
        _pulse(),
        dt=1.0,
        config=config,
        profile=None,
        published_drop_m=850.0,
        published_runout_m=2950.0,
    )
    b = seismic_impulse_estimate(_pulse(), dt=1.0, config=config)
    inflated = type(a)(
        name=a.name,
        method=a.method,
        mass_kg_p05=b.mass_kg_p05 * 50,
        mass_kg_p50=b.mass_kg_p50 * 50,
        mass_kg_p95=b.mass_kg_p95 * 50,
        a_eff=a.a_eff,
        assumptions=list(a.assumptions),
    )
    combined = combine(inflated, b)
    assert combined.consistency_ratio is not None and combined.consistency_ratio > 3
    assert any("disagree by more than a factor of three" in t for t in combined.assumptions)


def test_a_point_mass_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="strict interval"):
        MassEstimate(
            mass_kg_p05=1e11,
            mass_kg_p50=1e11,
            mass_kg_p95=1e11,
            method="combined",
            assumptions=["should never be reachable"],
        )
