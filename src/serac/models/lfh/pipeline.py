"""`invert_event(...) -> ForceHistory`: the whole M2 lane, end to end.

The order matters and is not negotiable:

1. prepare the waveforms and select the contributing channels;
2. **check the geometry and refuse if it cannot support a location** -- before any inversion
   runs, so a good-looking fit can never argue the refusal away;
3. grid-search the location on a coarse force basis (gSF);
4. re-invert at full resolution at the chosen node, with lambda from the L-curve;
5. estimate the mass twice, once against the DEM and once from the waveform alone;
6. bootstrap for the 5-95% envelope and every published interval;
7. assemble a `ForceHistory`, whose validators then check that the envelope really does
   bracket the median and that the mass really is an interval.

A refusal is a first-class outcome, not an error: it returns a `ForceHistory` with
`status="failed"`, the geometry stated in `notes`, and **no location**.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np

from serac.domain.detection import DetectionLocation
from serac.domain.force_history import (
    BootstrapInfo,
    ForceHistory,
    GreensProvenance,
    Interval,
    MassEstimate,
)
from serac.domain.seismic import Sncl
from serac.models.lfh.bootstrap import (
    BootstrapDraws,
    azimuth_interval,
    interval_of,
    published_mass,
    run_bootstrap,
)
from serac.models.lfh.cache import load as load_prepared
from serac.models.lfh.cache import preparation_key
from serac.models.lfh.cache import store as store_prepared
from serac.models.lfh.config import LfhConfig
from serac.models.lfh.gsf import (
    GreensCache,
    GridSearchResult,
    TrialLocation,
    grid_search,
    kernels_for,
)
from serac.models.lfh.inversion import InversionResult, invert
from serac.models.lfh.mass import (
    EstimatorResult,
    ForceSummary,
    dem_trajectory_estimate,
    seismic_impulse_estimate,
    summarise,
)
from serac.models.lfh.references import LfhTarget
from serac.models.lfh.trajectory import (
    TerrainProfile,
    TerrainUnavailableError,
    read_terrain_profile,
    runout_azimuth,
)
from serac.models.lfh.waveforms import (
    Geometry,
    StationChannel,
    geometry_of,
    prepare_channels,
    read_event_waveforms,
    refusal_reason,
    select_channels,
    station_weights,
)
from serac.ports.greens import GreensLibrary
from serac.ports.ledger import ManifestLedger

INVERSION_METHOD = "gSF grid search + second-difference Tikhonov single-force least squares"

#: How far along the force azimuth the DEM profile is sampled. Long enough for a Himalayan
#: runout, short enough to stay inside the committed GLO-30 crops.
DEM_PROFILE_LENGTH_M = 20_000.0


@dataclass
class InversionRun:
    """Everything the run produced, including the parts that do not fit the contract.

    `ForceHistory` is the published artefact; this carries the diagnostics a report needs --
    the L-curve, the variance-reduction surface, the per-station geometry, the timings and
    every note about a dropped channel.
    """

    force_history: ForceHistory
    target: LfhTarget
    config: LfhConfig
    geometry: Geometry | None = None
    channels: list[StationChannel] = field(default_factory=list)
    grid: GridSearchResult | None = None
    final: InversionResult | None = None
    summary: ForceSummary | None = None
    draws: BootstrapDraws | None = None
    estimator_a: EstimatorResult | None = None
    estimator_b: EstimatorResult | None = None
    terrain: TerrainProfile | None = None
    notes: list[str] = field(default_factory=list)
    timings_s: dict[str, float] = field(default_factory=dict)
    greens_cache_keys: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.force_history.status == "failed"


class _Timer:
    def __init__(self, timings: dict[str, float], name: str) -> None:
        self.timings, self.name = timings, name

    def __enter__(self) -> _Timer:
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.timings[self.name] = time.perf_counter() - self.start


def _sncls(channels: list[StationChannel]) -> list[Sncl]:
    seen: dict[str, Sncl] = {}
    for channel in channels:
        if channel.key not in seen:
            seen[channel.key] = Sncl(
                network=channel.network,
                station=channel.station,
                location=channel.location,
                channel=channel.channel,
            )
    return [seen[key] for key in sorted(seen)]


def _terrain_profile(
    target: LfhTarget,
    repo: Path,
    location: TrialLocation,
    azimuth_deg: float,
    max_distance_m: float,
    notes: list[str],
) -> TerrainProfile | None:
    if target.dem_fixture is None:
        notes.append(
            f"No DEM crop is committed for {target.target_id}, so the DEM-trajectory mass "
            "estimator falls back to a published fall height and runout."
        )
        return None
    path = repo / target.dem_fixture
    if not path.exists():
        notes.append(f"DEM fixture {target.dem_fixture} is missing; the terrain solve was skipped.")
        return None
    try:
        return read_terrain_profile(
            path,
            source_lat=location.latitude,
            source_lon=location.longitude,
            azimuth_deg=azimuth_deg,
            max_distance_m=max_distance_m,
        )
    except TerrainUnavailableError as exc:
        notes.append(f"DEM profile unavailable: {exc}")
        return None


def _refusal(
    target: LfhTarget, config: LfhConfig, geometry: Geometry | None, reason: str, notes: list[str]
) -> ForceHistory:
    """A `status='failed'` history: geometry stated, and deliberately no location."""
    detail = (
        f"REFUSED: {reason}. serac does not publish a source location it cannot support. "
        "No location, no mass and no force history are reported for this event."
    )
    return ForceHistory(
        status="failed",
        event_id=target.event_id or target.target_id,
        azimuthal_gap_deg=geometry.azimuthal_gap_deg if geometry else None,
        notes=" ".join([detail, *notes])[:8000],
    )


def invert_event(
    target: LfhTarget,
    *,
    repo: Path,
    library: GreensLibrary,
    ledger: ManifestLedger,
    config: LfhConfig,
    progress: Callable[[int], None] | None = None,
    prepared_cache_dir: Path | None = None,
) -> InversionRun:
    """Run the whole lane for one target and return the published history plus diagnostics.

    `prepared_cache_dir` memoises response removal, which is the slow non-linear-algebra part
    and the difference between the cold and warm latencies in the model card. It is optional
    and the run is identical without it.
    """
    timings: dict[str, float] = {}
    notes: list[str] = []

    with _Timer(timings, "prepare_s"):
        cache_key = None
        prepared = None
        if prepared_cache_dir is not None:
            cache_key = preparation_key(
                repo / target.fixture_dir,
                origin_iso=target.origin_utc.isoformat(),
                source_lat=target.source_latitude,
                source_lon=target.source_longitude,
                config=config,
            )
            cached = load_prepared(prepared_cache_dir, cache_key)
            if cached is not None:
                prepared = cached
                timings["prepare_cache_hit"] = 1.0
        if prepared is None:
            stream, inventory = read_event_waveforms(repo / target.fixture_dir)
            prepared, prep_notes = prepare_channels(
                stream,
                inventory,
                origin_utc=target.origin_utc,
                source_lat=target.source_latitude,
                source_lon=target.source_longitude,
                config=config,
            )
            notes.extend(prep_notes)
            if prepared_cache_dir is not None and cache_key is not None:
                store_prepared(prepared_cache_dir, cache_key, prepared)
        channels, select_notes = select_channels(prepared, config)
        notes.extend(select_notes)

    geometry = geometry_of(channels) if channels else None
    if geometry is None:
        return InversionRun(
            force_history=_refusal(target, config, None, "no channels survived preparation", notes),
            target=target,
            config=config,
            notes=notes,
            timings_s=timings,
        )
    reason = refusal_reason(geometry, config)
    if reason is not None:
        return InversionRun(
            force_history=_refusal(target, config, geometry, reason, notes),
            target=target,
            config=config,
            geometry=geometry,
            channels=channels,
            notes=notes,
            timings_s=timings,
        )

    weights = station_weights(channels)
    cache = GreensCache(library, ledger, config)

    with _Timer(timings, "grid_search_s"):
        grid = grid_search(
            channels,
            centre_lat=target.source_latitude,
            centre_lon=target.source_longitude,
            config=config,
            cache=cache,
            weights=weights,
            progress=progress,
        )

    if grid.variance_reduction < config.stations.min_variance_reduction:
        return InversionRun(
            force_history=_refusal(
                target,
                config,
                geometry,
                (
                    f"the best-fitting trial location explains only "
                    f"{grid.variance_reduction:.3f} of the data variance, below the floor of "
                    f"{config.stations.min_variance_reduction:.2f}; {geometry.describe()}. "
                    "A least-squares inversion of records that do not contain the signal still "
                    "returns a smooth force history with a clean envelope, and an amplitude set "
                    "by noise rather than by the event, so serac reports nothing"
                ),
                notes,
            ),
            target=target,
            config=config,
            geometry=geometry,
            channels=channels,
            grid=grid,
            notes=notes,
            timings_s=timings,
            greens_cache_keys=cache.used_keys,
        )

    best = grid.best.location
    with _Timer(timings, "final_inversion_s"):
        reg = config.regularisation
        final = invert(
            kernels_for(channels, best, cache, weights),
            n_basis=config.n_source_samples,
            stride=1,
            shift=grid.shift,
            dt=config.dt_s,
            zero_endpoints=reg.zero_endpoints,
            lambda_min=reg.lambda_min,
            lambda_max=reg.lambda_max,
            n_lambda=reg.n_lambda,
        )
    summary = summarise(final.forces, dt=config.dt_s)
    if final.l_curve is not None:
        corner = final.l_curve.corner_index
        if corner <= 1 or corner >= final.l_curve.lambdas.size - 2:
            notes.append(
                "The L-curve corner sits at the edge of the searched lambda range, so the "
                "regularisation weight is not well determined by the data."
            )

    with _Timer(timings, "terrain_s"):
        # The profile follows the direction the mass *moved*, which is opposite the force it
        # exerted: a = -F/M. Sampling along the force azimuth instead points the profile
        # uphill, and the terrain solve then has no solution at all -- which is how this was
        # caught. `read_terrain_profile` truncates at the first sample that leaves the raster,
        # so nothing beyond the committed crop is invented.
        runout_azimuth_deg = runout_azimuth(final.forces, dt=config.dt_s)
        notes.append(
            f"Force azimuth {summary.azimuth_deg:.0f} deg; the mass moved towards "
            f"{runout_azimuth_deg:.0f} deg, which is the bearing the DEM profile follows."
        )
        terrain = _terrain_profile(
            target,
            repo,
            best,
            runout_azimuth_deg,
            max_distance_m=DEM_PROFILE_LENGTH_M,
            notes=notes,
        )

    estimator_a = dem_trajectory_estimate(
        final.forces,
        dt=config.dt_s,
        config=config.mass,
        profile=terrain,
        published_drop_m=target.fall_height_m,
        published_runout_m=target.runout_m,
        published_source=target.geometry_source_ref,
    )
    estimator_b = seismic_impulse_estimate(final.forces, dt=config.dt_s, config=config.mass)

    with _Timer(timings, "bootstrap_s"):
        draws = run_bootstrap(
            channels,
            location=best,
            config=config,
            cache=cache,
            weights=weights,
            lambda_value=final.lambda_value,
            profile=terrain,
            published_drop_m=target.fall_height_m,
            published_runout_m=target.runout_m,
            published_source=target.geometry_source_ref,
        )
    boot_a, boot_b, mass = published_mass(draws, estimator_a, estimator_b)

    force_history = _assemble(
        target=target,
        config=config,
        channels=channels,
        geometry=geometry,
        grid=grid,
        final=final,
        draws=draws,
        mass=mass,
        weights=weights,
        cache=cache,
        notes=notes,
    )
    return InversionRun(
        force_history=force_history,
        target=target,
        config=config,
        geometry=geometry,
        channels=channels,
        grid=grid,
        final=final,
        summary=summary,
        draws=draws,
        estimator_a=boot_a,
        estimator_b=boot_b,
        terrain=terrain,
        notes=notes,
        timings_s=timings,
        greens_cache_keys=cache.used_keys,
    )


def _assemble(
    *,
    target: LfhTarget,
    config: LfhConfig,
    channels: list[StationChannel],
    geometry: Geometry,
    grid: GridSearchResult,
    final: InversionResult,
    draws: BootstrapDraws,
    mass: MassEstimate,
    weights: dict[str, float],
    cache: GreensCache,
    notes: list[str],
) -> ForceHistory:
    p05, p50, p95 = draws.envelope()
    peak = interval_of(draws.peak_force_n)
    impulse = interval_of(draws.impulse_ns)
    duration = interval_of(draws.duration_s)
    azimuth = azimuth_interval(draws.azimuth_deg)

    best = grid.best.location
    location = DetectionLocation(
        latitude=best.latitude,
        longitude=best.longitude,
        depth_km=best.depth_m / 1000.0,
        uncertainty_radius_km=max(grid.uncertainty_radius_km(), config.grid.spacing_km),
        method="gsf_grid_search",
        grid_spacing_km=config.grid.spacing_km,
        variance_reduction=float(np.clip(grid.variance_reduction, 0.0, 1.0)),
        azimuthal_gap_deg=geometry.azimuthal_gap_deg,
    )
    start = target.origin_utc - timedelta(seconds=config.source_lead_s)
    summary_note = (
        f"gSF over {len(grid.nodes)} trial nodes at {config.grid.spacing_km:g} km spacing; "
        f"best variance reduction {grid.variance_reduction:.3f}. "
        f"{geometry.describe()}. "
        f"Lambda {final.lambda_value:.4g} from the L-curve corner. "
        "Green's functions are modelled (Syngine, PREM), never observed, and are not "
        "published on the bus."
    )
    return ForceHistory(
        status="computed",
        event_id=target.event_id or target.target_id,
        sncls=_sncls(channels),
        station_weights={key: round(value, 6) for key, value in sorted(weights.items())},
        time_start_utc=start,
        sample_interval_s=config.dt_s,
        n_samples=int(p50.shape[1]),
        force_up_n=p50[0].tolist(),
        force_north_n=p50[1].tolist(),
        force_east_n=p50[2].tolist(),
        force_up_p05_n=p05[0].tolist(),
        force_up_p95_n=p95[0].tolist(),
        force_north_p05_n=p05[1].tolist(),
        force_north_p95_n=p95[1].tolist(),
        force_east_p05_n=p05[2].tolist(),
        force_east_p95_n=p95[2].tolist(),
        source_location=location,
        variance_reduction=location.variance_reduction,
        azimuthal_gap_deg=geometry.azimuthal_gap_deg,
        peak_force_n=Interval(p05=peak[0], p50=peak[1], p95=peak[2], units="N"),
        impulse_ns=Interval(p05=impulse[0], p50=impulse[1], p95=impulse[2], units="N s"),
        duration_s=Interval(p05=duration[0], p50=duration[1], p95=duration[2], units="s"),
        force_azimuth_deg=Interval(
            p05=azimuth[0],
            p50=azimuth[1],
            p95=azimuth[2],
            units="deg from north (unwrapped: an interval spanning north runs past 0 or 360)",
        ),
        mass=mass,
        greens=GreensProvenance(
            earth_model=config.earth_model.value,
            provider="IRIS Syngine",
            provider_url="https://service.iris.edu/irisws/syngine/1/query",
            dt_s=config.dt_s,
            band_s=(config.band.short_period_s, config.band.long_period_s),
            cache_sha256=cache.used_sha256,
        ),
        regularisation=(
            f"second-difference Tikhonov, order {config.regularisation.order}, zero endpoints"
        ),
        lambda_value=final.lambda_value,
        bootstrap=BootstrapInfo(
            n_draws=draws.n_draws,
            seed=config.bootstrap.seed,
            resampled=["stations", "band_limits", "lambda", "source_depth", "friction"],
        ),
        inversion_method=INVERSION_METHOD,
        notes=" ".join([summary_note, *notes])[:8000],
    )
