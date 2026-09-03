"""Two independent mass estimators, and the summary quantities they stand on.

serac never publishes a point mass. `MassEstimate` refuses to construct one, and this module
supplies two estimators rather than one so that the interval reflects genuine disagreement
between methods rather than the spread of a single method's noise:

**A -- `fmax_over_aeff`, DEM-trajectory.** The classic `M = F_max / a_eff`. The effective
acceleration comes from a trajectory-consistent kinematic model: the force history is
integrated to a unit-mass path, that path is laid on the real DEM from the inverted source
location, and the mass at which the modelled drop matches the ground's own drop fixes the
path angle. Then `a_eff = g (sin theta - mu cos theta)` with `mu` an interval. Where no DEM
covers the runout, the path angle falls back to a published fall height and runout, and the
`AEff.basis` drops from `dem_trajectory` to `assumed_range` so the weaker input is visible in
the output.

**B -- `impulse_over_velocity`, seismically self-contained.** Uses no DEM, no catalogue and no
published geometry. The running impulse `p(t) = integral F dt'` is the slide's momentum, so
its peak is `M v_max`; the path angle is read from the force history itself, because on a
slope the reaction force has vertical part `M a sin(theta)` and horizontal part
`M a cos(theta)`; and `v_max = a_eff t_acc`. Hence `M = max|p| / (a_eff t_acc)`.

The two are **not independent**: both go through `a_eff` and therefore through the same
friction assumption. They differ in the force functional (peak versus integral) and in where
the geometry comes from (terrain versus waveform). That is a real but partial independence
and it is stated in `assumptions[]` rather than implied away. The published estimate is the
union of the two intervals plus the consistency ratio of their medians.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from serac.domain.force_history import AEff, Interval, MassEstimate
from serac.models.lfh.config import MassConfig
from serac.models.lfh.trajectory import (
    TerrainProfile,
    TerrainUnavailableError,
    mass_from_terrain,
    path_angle_from_force,
    slope_from_drop_and_runout,
)


@dataclass(frozen=True)
class ForceSummary:
    """Scalar quantities read off a force history."""

    peak_force_n: float
    peak_index: int
    impulse_ns: float
    impulse_peak_index: int
    onset_index: int
    duration_s: float
    acceleration_time_s: float
    azimuth_deg: float
    path_angle_deg: float

    @property
    def is_usable(self) -> bool:
        return (
            self.peak_force_n > 0
            and self.impulse_ns > 0
            and self.acceleration_time_s > 0
            and self.duration_s > 0
        )


def summarise(forces_n: np.ndarray, *, dt: float, onset_fraction: float = 0.05) -> ForceSummary:
    """Peak force, peak running impulse, onset, duration and mean horizontal azimuth.

    Duration is the span over which the force magnitude stays above `onset_fraction` of its
    peak. It is a threshold definition, not a physical one, and the threshold is reported.
    """
    magnitude = np.linalg.norm(forces_n, axis=0)
    peak = float(magnitude.max()) if magnitude.size else 0.0
    if peak <= 0:
        return ForceSummary(0.0, 0, 0.0, 0, 0, 0.0, 0.0, 0.0, 90.0)
    peak_index = int(np.argmax(magnitude))
    above = np.nonzero(magnitude >= onset_fraction * peak)[0]
    onset_index = int(above[0])
    duration_s = float((int(above[-1]) - onset_index) * dt)

    running = np.cumsum(forces_n, axis=1) * dt
    running_magnitude = np.linalg.norm(running, axis=0)
    impulse_peak_index = int(np.argmax(running_magnitude))
    impulse = float(running_magnitude[impulse_peak_index])
    acceleration_time_s = float(max(impulse_peak_index - onset_index, 1) * dt)

    weights = magnitude**2
    north = float(np.sum(forces_n[1] * weights))
    east = float(np.sum(forces_n[2] * weights))
    azimuth = float(math.degrees(math.atan2(east, north)) % 360.0)
    return ForceSummary(
        peak_force_n=peak,
        peak_index=peak_index,
        impulse_ns=impulse,
        impulse_peak_index=impulse_peak_index,
        onset_index=onset_index,
        duration_s=duration_s,
        acceleration_time_s=acceleration_time_s,
        azimuth_deg=azimuth,
        path_angle_deg=path_angle_from_force(forces_n),
    )


def effective_acceleration(path_angle_deg: float, friction_ratio: float, gravity: float) -> float:
    """Down-slope acceleration with basal friction expressed relative to the slope.

    Coulomb friction gives `a = g (sin theta - mu cos theta)`. Writing `mu = phi tan(theta)`,
    where `phi` is the basal friction as a fraction of the apparent (Heim-ratio) friction, the
    expression collapses to

        a_eff = g sin(theta) (1 - phi)

    which is positive for every slope by construction. That is not a convenience: an absolute
    friction coefficient larger than `tan(theta)` describes a mass that cannot move, and
    sampling one drives `a_eff` to a numerical floor and the mass to absurdity. Bingham Canyon
    (H/L = 0.29) sits below the middle of a plausible-looking absolute range, which is how the
    error stays invisible until the mass comes out ten times too large.
    """
    theta = math.radians(path_angle_deg)
    value = gravity * math.sin(theta) * (1.0 - friction_ratio)
    return max(value, 0.05)


@dataclass
class EstimatorResult:
    """One estimator's answer, with everything it assumed."""

    name: str
    method: str
    mass_kg_p05: float
    mass_kg_p50: float
    mass_kg_p95: float
    a_eff: AEff
    assumptions: list[str] = field(default_factory=list)
    diagnostics: dict[str, float] = field(default_factory=dict)

    def as_estimate(self) -> MassEstimate:
        return MassEstimate(
            mass_kg_p05=self.mass_kg_p05,
            mass_kg_p50=self.mass_kg_p50,
            mass_kg_p95=self.mass_kg_p95,
            method=self.method,  # type: ignore[arg-type]
            a_eff=self.a_eff,
            assumptions=list(self.assumptions),
        )


def _friction_interval(config: MassConfig) -> Interval:
    return Interval(
        p05=config.friction_ratio_min,
        p50=0.5 * (config.friction_ratio_min + config.friction_ratio_max),
        p95=config.friction_ratio_max,
        units="basal friction as a fraction of tan(path angle)",
    )


def _spread(values: list[float]) -> tuple[float, float, float]:
    """Strict `(p05, p50, p95)`; a degenerate spread is widened rather than collapsed.

    `MassEstimate` rejects `p05 == p50`, and rightly: a point mass is not publishable. When
    the resampling genuinely produces no spread -- a single friction value, say -- the
    interval is widened by a stated factor rather than the validator being worked around.
    """
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array) & (array > 0)]
    if array.size == 0:
        raise ValueError("no finite positive mass samples")
    p05, p50, p95 = (float(v) for v in np.percentile(array, [5, 50, 95]))
    if not p05 < p50:
        p05 = p50 / 1.5
    if not p50 < p95:
        p95 = p50 * 1.5
    return p05, p50, p95


def dem_trajectory_estimate(
    forces_n: np.ndarray,
    *,
    dt: float,
    config: MassConfig,
    profile: TerrainProfile | None,
    published_drop_m: float | None = None,
    published_runout_m: float | None = None,
    published_source: str | None = None,
    n_friction: int = 33,
) -> EstimatorResult:
    """Estimator A: `M = F_max / a_eff`, path angle from the terrain."""
    summary = summarise(forces_n, dt=dt)
    if not summary.is_usable:
        raise ValueError("force history carries no usable peak; cannot estimate a mass")

    assumptions = [
        "M = F_max / a_eff with a_eff = g sin(theta) (1 - phi): a rigid block on a planar "
        "slope under Coulomb friction, which ignores internal deformation, entrainment and "
        "any change of basal resistance along the path.",
        f"Basal friction is expressed as phi = mu / tan(theta), sampled uniformly over "
        f"[{config.friction_ratio_min}, {config.friction_ratio_max}]. Friction below the "
        "apparent (Heim-ratio) friction is what makes the mass accelerate at all; an absolute "
        "coefficient is not used because one larger than tan(theta) describes a mass that "
        "cannot move. The range is not calibrated to any event in this repository.",
        f"g = {config.gravity_m_s2} m/s^2.",
    ]
    terrain_mass: float | None = None
    if profile is not None:
        try:
            solution = mass_from_terrain(forces_n, profile, dt=dt)
        except TerrainUnavailableError as exc:
            profile = None
            assumptions.append(f"DEM trajectory solve failed and was not used: {exc}")
        else:
            path_angle = solution.path_angle_deg
            terrain_mass = solution.mass_kg
            basis = "dem_trajectory"
            assumptions.append(
                "Path angle from a trajectory-consistent solve on the DEM: the force history "
                "was integrated to a unit-mass path, laid on "
                f"{profile.dem_path} from the inverted source location along bearing "
                f"{profile.azimuth_deg:.0f} deg, and the mass fixed by requiring the modelled "
                f"drop ({solution.modelled_drop_m:.0f} m) to match the ground's drop "
                f"({solution.terrain_drop_m:.0f} m) over the corresponding horizontal runout "
                f"({solution.horizontal_runout_m:.0f} m)."
            )
            if not solution.converged:
                assumptions.append(
                    "The DEM trajectory solve hit its iteration limit; the path angle is "
                    "approximate."
                )
            if solution.horizontal_runout_m >= profile.max_distance_m:
                assumptions.append(
                    f"The trajectory reached the edge of the DEM crop at "
                    f"{profile.max_distance_m:.0f} m, so the terrain drop is a lower bound and "
                    "the mass is biased upwards."
                )
    if profile is None:
        basis = "assumed_range"
        if published_drop_m is not None and published_runout_m is not None:
            path_angle = slope_from_drop_and_runout(published_drop_m, published_runout_m)
            assumptions.append(
                "No DEM crop covers this runout, so the path angle is atan(H/L) from a "
                f"published fall height {published_drop_m:.0f} m and runout "
                f"{published_runout_m:.0f} m"
                + (f" ({published_source})" if published_source else "")
                + ". This is a weaker input than a terrain profile and AEff.basis records it "
                "as assumed_range rather than dem_trajectory."
            )
        else:
            # Last resort. The estimator still runs, but it no longer brings independent
            # geometry, so it stops being independent of estimator B. Saying so matters more
            # than the number: the consistency ratio below is then near-vacuous.
            path_angle = path_angle_from_force(forces_n)
            assumptions.append(
                "DEGRADED: neither a usable DEM profile nor a published fall height and "
                "runout was available, so the path angle falls back to the force history "
                "itself -- the same geometry estimator B uses. The two estimators are then "
                "no longer independent in their geometry and their consistency ratio "
                "measures only the difference between a peak-force and an impulse functional."
            )

    path_angle = float(min(max(path_angle, config.min_path_angle_deg), config.max_path_angle_deg))
    frictions = np.linspace(config.friction_ratio_min, config.friction_ratio_max, n_friction)
    masses = [
        summary.peak_force_n / effective_acceleration(path_angle, float(phi), config.gravity_m_s2)
        for phi in frictions
    ]
    p05, p50, p95 = _spread(masses)
    diagnostics = {
        "path_angle_deg": path_angle,
        "peak_force_n": summary.peak_force_n,
        "a_eff_median_m_s2": effective_acceleration(
            path_angle, float(np.median(frictions)), config.gravity_m_s2
        ),
    }
    if terrain_mass is not None:
        diagnostics["terrain_kinematic_mass_kg"] = terrain_mass
        assumptions.append(
            "For reference, the purely kinematic terrain solve -- which uses no friction "
            f"assumption at all -- put the mass at {terrain_mass:.2e} kg."
        )
    return EstimatorResult(
        name="dem_trajectory",
        method="fmax_over_aeff",
        mass_kg_p05=p05,
        mass_kg_p50=p50,
        mass_kg_p95=p95,
        a_eff=AEff(
            value_m_s2=diagnostics["a_eff_median_m_s2"],
            basis=basis,  # type: ignore[arg-type]
            slope_deg=path_angle,
            friction_coefficient=_friction_interval(config),
            notes=(
                "Path angle from the DEM trajectory solve."
                if basis == "dem_trajectory"
                else "Path angle from a published fall height and runout; no DEM was available."
            ),
        ),
        assumptions=assumptions,
        diagnostics=diagnostics,
    )


def seismic_impulse_estimate(
    forces_n: np.ndarray, *, dt: float, config: MassConfig, n_friction: int = 33
) -> EstimatorResult:
    """Estimator B: `M = max|integral F dt| / (a_eff t_acc)`, geometry from the waveform."""
    summary = summarise(forces_n, dt=dt)
    if not summary.is_usable:
        raise ValueError("force history carries no usable impulse; cannot estimate a mass")

    path_angle = float(
        min(max(summary.path_angle_deg, config.min_path_angle_deg), config.max_path_angle_deg)
    )
    frictions = np.linspace(config.friction_ratio_min, config.friction_ratio_max, n_friction)
    masses = [
        summary.impulse_ns
        / (
            effective_acceleration(path_angle, float(phi), config.gravity_m_s2)
            * summary.acceleration_time_s
        )
        for phi in frictions
    ]
    p05, p50, p95 = _spread(masses)
    a_eff_median = effective_acceleration(
        path_angle, float(np.median(frictions)), config.gravity_m_s2
    )
    assumptions = [
        "M = max|integral F dt| / (a_eff * t_acc): the peak of the running impulse is the "
        "slide's peak momentum M*v_max, and v_max is taken as a_eff times the time from "
        "onset to that peak.",
        "The path angle comes from the force history alone -- theta = atan(|F_vertical| / "
        "|F_horizontal|) at the instant of peak horizontal force -- so this estimator uses no "
        "DEM, no catalogue and no published geometry.",
        f"Basal friction as a fraction of tan(theta) sampled uniformly over "
        f"[{config.friction_ratio_min}, {config.friction_ratio_max}].",
        "Constant a_eff through the acceleration phase, which a real slide on changing terrain "
        "does not have.",
        f"Onset threshold 5% of peak force; acceleration phase measured as "
        f"{summary.acceleration_time_s:.0f} s.",
    ]
    return EstimatorResult(
        name="seismic_impulse",
        method="impulse_over_velocity",
        mass_kg_p05=p05,
        mass_kg_p50=p50,
        mass_kg_p95=p95,
        a_eff=AEff(
            value_m_s2=a_eff_median,
            basis="assumed_range",
            slope_deg=path_angle,
            friction_coefficient=_friction_interval(config),
            notes=(
                "Path angle read from the inverted force history; no external geometry was used."
            ),
        ),
        assumptions=assumptions,
        diagnostics={
            "path_angle_deg": path_angle,
            "impulse_ns": summary.impulse_ns,
            "acceleration_time_s": summary.acceleration_time_s,
            "a_eff_median_m_s2": a_eff_median,
        },
    )


def combine(
    a: EstimatorResult, b: EstimatorResult, *, extra_assumptions: list[str] | None = None
) -> MassEstimate:
    """The published estimate: the union of the two intervals, plus the consistency ratio.

    Taking the union rather than an average is deliberate. The two estimators disagree in ways
    neither one's internal spread captures, and narrowing that disagreement into a tighter
    number would be exactly the false precision this repository exists to avoid.
    """
    p05 = min(a.mass_kg_p05, b.mass_kg_p05)
    p95 = max(a.mass_kg_p95, b.mass_kg_p95)
    p50 = math.sqrt(a.mass_kg_p50 * b.mass_kg_p50)
    p50 = float(min(max(p50, p05 * 1.0000001), p95 * 0.9999999))
    if not p05 < p50 < p95:  # pragma: no cover - only when both estimators degenerate
        p05, p95 = p50 / 1.5, p50 * 1.5
    ratio = a.mass_kg_p50 / b.mass_kg_p50

    assumptions = [
        f"Published interval is the UNION of two estimators, not their average: "
        f"{a.name} ({a.method}) gave [{a.mass_kg_p05:.2e}, {a.mass_kg_p95:.2e}] kg and "
        f"{b.name} ({b.method}) gave [{b.mass_kg_p05:.2e}, {b.mass_kg_p95:.2e}] kg.",
        f"Consistency ratio M({a.name}) / M({b.name}) = {ratio:.2f} on the medians.",
        "The two estimators are NOT independent: both divide by an effective acceleration "
        "built from the same Coulomb friction range. They differ in the force functional "
        "(peak versus integral) and in the source of the path geometry (terrain versus "
        "waveform), so agreement is evidence but not proof.",
        "The median is the geometric mean of the two estimators' medians, which is a summary "
        "of two methods rather than a measurement.",
    ]
    if not 1 / 3 <= ratio <= 3:
        assumptions.append(
            f"The estimators disagree by more than a factor of three (ratio {ratio:.2f}). "
            "This is reported, not reconciled: the union interval is correspondingly wide and "
            "the mass should be treated as order-of-magnitude only."
        )
    assumptions.extend(a.assumptions)
    assumptions.extend(b.assumptions)
    if extra_assumptions:
        assumptions.extend(extra_assumptions)
    return MassEstimate(
        mass_kg_p05=p05,
        mass_kg_p50=p50,
        mass_kg_p95=p95,
        method="combined",
        a_eff=a.a_eff,
        consistency_ratio=ratio,
        assumptions=assumptions,
    )
