"""Uncertainty by resampling the choices that were not forced on us.

A force history has no error bars of its own. The interval serac publishes comes from redoing
the inversion many times while varying the things a different analyst could reasonably have
chosen differently:

* **stations** -- resampled with replacement, which is the only term that reflects how much
  the answer depends on which networks happened to be recording;
* **band limits** -- both corners jittered log-uniformly, because 20-150 s is a convention
  rather than a measurement;
* **lambda** -- jittered log-uniformly around the L-curve corner, because the corner is a
  criterion, not a fact;
* **source depth** -- drawn from the configured set, because a mass movement is a surface
  process being represented in a model that wants a depth;
* **friction** -- drawn from the configured range, which is what carries the mass estimate's
  spread.

Two honest limits. The draws are not independent of each other, so the interval is a spread
over analyst choices rather than a posterior. And nothing here resamples the Earth model:
using PREM instead of ak135 would move the answer by an amount this bootstrap cannot see, and
the model card says so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from serac.domain.force_history import MassEstimate
from serac.models.lfh.config import LfhConfig
from serac.models.lfh.gsf import GreensCache, TrialLocation, kernels_for
from serac.models.lfh.inversion import TraceKernel, invert
from serac.models.lfh.mass import (
    EstimatorResult,
    combine,
    dem_trajectory_estimate,
    seismic_impulse_estimate,
    summarise,
)
from serac.models.lfh.trajectory import TerrainProfile
from serac.models.lfh.waveforms import StationChannel, bandpass


@dataclass
class BootstrapDraws:
    """Every draw's force history and scalars, kept so percentiles are computed once."""

    forces: np.ndarray  # (n_draws, 3, n_samples)
    peak_force_n: list[float] = field(default_factory=list)
    impulse_ns: list[float] = field(default_factory=list)
    duration_s: list[float] = field(default_factory=list)
    azimuth_deg: list[float] = field(default_factory=list)
    variance_reduction: list[float] = field(default_factory=list)
    lambda_value: list[float] = field(default_factory=list)
    mass_a_kg: list[float] = field(default_factory=list)
    mass_b_kg: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def n_draws(self) -> int:
        return int(self.forces.shape[0])

    def envelope(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """`(p05, p50, p95)` of the force arrays, sample by sample and component by component.

        Percentiles are taken independently per sample, so the envelope brackets the median
        everywhere by construction -- which is exactly what `ForceHistory` requires -- while
        not being a confidence band for any single realisation.
        """
        p05 = np.percentile(self.forces, 5, axis=0)
        p50 = np.percentile(self.forces, 50, axis=0)
        p95 = np.percentile(self.forces, 95, axis=0)
        p05 = np.minimum(p05, p50)
        p95 = np.maximum(p95, p50)
        return p05, p50, p95


def _circular_percentiles(degrees: list[float]) -> tuple[float, float, float]:
    """Percentiles of an azimuth set, taken about its own circular mean, left unwrapped.

    Averaging 359 and 1 degrees to 180 is the classic way to publish a nonsense bearing, so
    the samples are unwrapped about the resultant direction before the percentiles are taken.
    The result is deliberately *not* wrapped back into [0, 360): an interval that spans north
    must stay ordered to be an interval at all, so it is reported as e.g. 350 / 358 / 366 and
    the units string says so.
    """
    if not degrees:
        return 0.0, 0.0, 0.0
    radians = np.radians(np.asarray(degrees, dtype=float))
    mean = float(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean()))
    deviations = np.degrees(np.angle(np.exp(1j * (radians - mean))))
    p05, p50, p95 = (float(v) for v in np.percentile(deviations, [5, 50, 95]))
    centre = float(np.degrees(mean) % 360.0)
    return centre + p05, centre + p50, centre + p95


def run_bootstrap(
    channels: list[StationChannel],
    *,
    location: TrialLocation,
    config: LfhConfig,
    cache: GreensCache,
    weights: dict[str, float],
    lambda_value: float,
    profile: TerrainProfile | None,
    published_drop_m: float | None = None,
    published_runout_m: float | None = None,
    published_source: str | None = None,
    progress: Callable[[int], None] | None = None,
) -> BootstrapDraws:
    """Resample and re-invert `config.bootstrap.n_draws` times at the chosen location."""
    settings = config.bootstrap
    rng = np.random.default_rng(settings.seed)
    n_fine = config.n_source_samples
    shift = config.greens_shift_samples

    by_station: dict[str, list[StationChannel]] = {}
    for channel in channels:
        by_station.setdefault(channel.station_key, []).append(channel)
    station_keys = sorted(by_station)

    forces: list[np.ndarray] = []
    draws = BootstrapDraws(forces=np.zeros((0, 3, n_fine)))
    for draw in range(settings.n_draws):
        if settings.resample_stations and len(station_keys) > 1:
            picked = rng.choice(len(station_keys), size=len(station_keys), replace=True)
            selected = [c for index in picked for c in by_station[station_keys[int(index)]]]
        else:  # pragma: no cover - configuration keeps resampling on
            selected = list(channels)

        short = config.band.short_period_s * float(
            np.exp(rng.uniform(-np.log(settings.band_jitter), np.log(settings.band_jitter)))
        )
        long = config.band.long_period_s * float(
            np.exp(rng.uniform(-np.log(settings.band_jitter), np.log(settings.band_jitter)))
        )
        if short >= long:  # pragma: no cover - jitter is far smaller than the band width
            short, long = config.band.short_period_s, config.band.long_period_s
        draw_config = config.model_copy(
            update={
                "band": config.band.model_copy(
                    update={"short_period_s": short, "long_period_s": long}
                )
            }
        )
        lam = lambda_value * float(
            np.exp(rng.uniform(-np.log(settings.lambda_jitter), np.log(settings.lambda_jitter)))
        )
        depth = float(rng.choice(np.asarray(settings.depths_m, dtype=float)))
        node = TrialLocation(location.latitude, location.longitude, depth)

        draw_cache = GreensCache(cache.library, cache.ledger, draw_config)
        cache.share_provenance_with(draw_cache)
        # Both sides of the equation are filtered at the draw's band: the Green's functions
        # inside `kernels_for` via `draw_config`, and the data here from the *broadband*
        # series rather than the already-filtered one, so nothing is filtered twice.
        rebanded = {
            channel.key: bandpass(channel.broadband, dt=config.dt_s, config=draw_config)
            for channel in selected
        }
        try:
            kernels = kernels_for(selected, node, draw_cache, weights)
            refiltered = [
                TraceKernel(
                    key=kernel.key,
                    component=kernel.component,
                    data=rebanded[kernel.key],
                    kernels=kernel.kernels,
                    weight=kernel.weight,
                    distance_deg=kernel.distance_deg,
                    azimuth_deg=kernel.azimuth_deg,
                )
                for kernel in kernels
            ]
            result = invert(
                refiltered,
                n_basis=n_fine,
                stride=1,
                shift=shift,
                dt=config.dt_s,
                zero_endpoints=config.regularisation.zero_endpoints,
                lambda_value=lam,
            )
        except (ValueError, np.linalg.LinAlgError) as exc:
            draws.failures.append(f"draw {draw}: {exc}")
            continue

        summary = summarise(result.forces, dt=config.dt_s)
        if not summary.is_usable:
            draws.failures.append(f"draw {draw}: force history carried no usable peak")
            continue
        forces.append(result.forces)
        draws.peak_force_n.append(summary.peak_force_n)
        draws.impulse_ns.append(summary.impulse_ns)
        draws.duration_s.append(summary.duration_s)
        draws.azimuth_deg.append(summary.azimuth_deg)
        draws.variance_reduction.append(result.variance_reduction)
        draws.lambda_value.append(result.lambda_value)

        friction = float(
            rng.uniform(config.mass.friction_ratio_min, config.mass.friction_ratio_max)
        )
        mass_config = config.mass.model_copy(
            update={
                "friction_ratio_min": max(friction - 1e-6, 1e-6),
                "friction_ratio_max": min(friction + 1e-6, 0.999999),
            }
        )
        try:
            estimate_a = dem_trajectory_estimate(
                result.forces,
                dt=config.dt_s,
                config=mass_config,
                profile=profile,
                published_drop_m=published_drop_m,
                published_runout_m=published_runout_m,
                published_source=published_source,
                n_friction=1,
            )
            estimate_b = seismic_impulse_estimate(
                result.forces, dt=config.dt_s, config=mass_config, n_friction=1
            )
        except ValueError as exc:
            draws.failures.append(f"draw {draw}: mass estimate failed ({exc})")
        else:
            draws.mass_a_kg.append(estimate_a.mass_kg_p50)
            draws.mass_b_kg.append(estimate_b.mass_kg_p50)
        if progress is not None:
            progress(draw + 1)

    if not forces:
        raise ValueError(
            "every bootstrap draw failed; the inversion has no uncertainty to report: "
            + "; ".join(draws.failures[:3])
        )
    draws.forces = np.stack(forces, axis=0)
    return draws


def interval_of(values: list[float]) -> tuple[float, float, float]:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if array.size == 0:  # pragma: no cover - guarded by run_bootstrap
        raise ValueError("no finite samples")
    p05, p50, p95 = (float(v) for v in np.percentile(array, [5, 50, 95]))
    return p05, p50, p95


def azimuth_interval(values: list[float]) -> tuple[float, float, float]:
    return _circular_percentiles(values)


def bootstrapped_mass(
    draws: BootstrapDraws,
    *,
    reference_a: EstimatorResult,
    reference_b: EstimatorResult,
) -> tuple[EstimatorResult, EstimatorResult]:
    """Replace each estimator's interval with its bootstrap spread, keeping its assumptions.

    The reference estimators supply `a_eff`, the method label and the assumption list; the
    numbers come from the draws, so station resampling and band jitter reach the mass rather
    than only the force history.
    """
    from serac.models.lfh.mass import _spread

    p05_a, p50_a, p95_a = _spread(draws.mass_a_kg)
    p05_b, p50_b, p95_b = _spread(draws.mass_b_kg)
    a = EstimatorResult(
        name=reference_a.name,
        method=reference_a.method,
        mass_kg_p05=p05_a,
        mass_kg_p50=p50_a,
        mass_kg_p95=p95_a,
        a_eff=reference_a.a_eff,
        assumptions=list(reference_a.assumptions),
        diagnostics=dict(reference_a.diagnostics),
    )
    b = EstimatorResult(
        name=reference_b.name,
        method=reference_b.method,
        mass_kg_p05=p05_b,
        mass_kg_p50=p50_b,
        mass_kg_p95=p95_b,
        a_eff=reference_b.a_eff,
        assumptions=list(reference_b.assumptions),
        diagnostics=dict(reference_b.diagnostics),
    )
    return a, b


def published_mass(
    draws: BootstrapDraws, reference_a: EstimatorResult, reference_b: EstimatorResult
) -> tuple[EstimatorResult, EstimatorResult, MassEstimate]:
    a, b = bootstrapped_mass(draws, reference_a=reference_a, reference_b=reference_b)
    extra = [
        f"Intervals are bootstrap percentiles over {draws.n_draws} draws resampling stations, "
        "band corners, lambda, source depth and friction; they are a spread over analyst "
        "choices, not a posterior.",
        "Nothing here resamples the 1-D Earth model: a different model would move the answer "
        "by an amount this bootstrap cannot see.",
    ]
    if draws.failures:
        extra.append(
            f"{len(draws.failures)} of {draws.n_draws + len(draws.failures)} bootstrap draws "
            f"failed and were excluded; first: {draws.failures[0]}"
        )
    return a, b, combine(a, b, extra_assumptions=extra)
