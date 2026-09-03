"""gSF: grid search over trial source locations by variance reduction.

The force-history inversion is linear once the location is fixed, but the location itself is
not: it enters through every station's distance and azimuth. The standard remedy is to invert
at each node of a grid of trial locations and keep the one whose fit explains the most data
variance -- "grid search single force", gSF.

Two things this module refuses to do:

* It will not return a location when the geometry cannot support one. The caller checks
  `refusal_reason` first; this module is only reached once the station set has passed.
* It will not report a variance reduction from a different regularisation than the one the
  final answer uses. Every node is solved at the same lambda, chosen once on the nominal
  location, so the comparison between nodes is like for like.

The search runs on a coarse piecewise-constant force basis (`stride > 1`). The location is
insensitive to fine force structure and the coarse basis makes 121 nodes affordable; the
final force history is then re-inverted at full resolution at the chosen node.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from serac.adapters.seismic.syngine import (
    as_arrays,
    geocentric_distance_azimuth,
    nearest_request,
    rotate_to_zne,
)
from serac.models.lfh.config import LfhConfig
from serac.models.lfh.inversion import TraceKernel, accumulate, solve_normal
from serac.models.lfh.waveforms import StationChannel, azimuthal_gap
from serac.ports.greens import GreensLibrary, GreensRequest
from serac.ports.ledger import ManifestLedger

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = 2.0 * math.pi * EARTH_RADIUS_KM / 360.0


@dataclass(frozen=True)
class TrialLocation:
    latitude: float
    longitude: float
    depth_m: float


def build_grid(centre_lat: float, centre_lon: float, config: LfhConfig) -> list[TrialLocation]:
    """A square grid of trial locations centred on the nominal source position.

    Spacing is in kilometres and converted to degrees at the centre latitude, so the grid is
    square on the ground rather than in coordinates.
    """
    grid = config.grid
    n = grid.n_per_side
    half = (n - 1) // 2
    dlat = grid.spacing_km / KM_PER_DEGREE
    cos_lat = max(math.cos(math.radians(centre_lat)), 1e-6)
    dlon = grid.spacing_km / (KM_PER_DEGREE * cos_lat)
    out: list[TrialLocation] = []
    for i in range(-half, half + 1):
        for j in range(-half, half + 1):
            for depth in grid.depths_m:
                out.append(
                    TrialLocation(
                        latitude=centre_lat + i * dlat,
                        longitude=centre_lon + j * dlon,
                        depth_m=depth,
                    )
                )
    return out


class GreensCache:
    """Band-passed elementary Green's functions, memoised by library cache key.

    The grid is far finer than the 0.05 deg Green's lattice, so many nodes snap to the same
    request. Filtering is the expensive part, so the filtered arrays are what is memoised.
    """

    def __init__(
        self,
        library: GreensLibrary,
        ledger: ManifestLedger,
        config: LfhConfig,
        *,
        bandpass_fn: object | None = None,
    ) -> None:
        self.library = library
        self.ledger = ledger
        self.config = config
        self._filtered: dict[str, dict[tuple[str, str], np.ndarray]] = {}
        self._requests: dict[str, GreensRequest] = {}
        self._sha256: dict[str, str] = {}

    def request_for(self, distance_deg: float, depth_m: float) -> GreensRequest:
        return nearest_request(
            distance_deg,
            model=self.config.earth_model,
            source_depth_m=depth_m,
            dt_s=self.config.dt_s,
            duration_s=self.config.greens_duration_s,
            step_deg=self.config.greens_step_deg,
            min_deg=self.config.stations.min_distance_deg,
            max_deg=self.config.stations.max_distance_deg,
        )

    def elementary(self, distance_deg: float, depth_m: float) -> dict[tuple[str, str], np.ndarray]:
        from serac.models.lfh.waveforms import bandpass

        request = self.request_for(distance_deg, depth_m)
        key = request.cache_key()
        cached = self._filtered.get(key)
        if cached is not None:
            return cached
        greens = self.library.get(request, self.ledger)
        raw = as_arrays(greens)
        filtered = {
            name: bandpass(values, dt=self.config.dt_s, config=self.config)
            for name, values in raw.items()
        }
        self._filtered[key] = filtered
        self._requests[key] = request
        self._sha256[key] = greens.sha256
        return filtered

    def share_provenance_with(self, other: GreensCache) -> None:
        """Let a derived cache record into the same sha256 map, so a bootstrap draw's
        Green's functions are provenance-tracked alongside the main run's."""
        other._sha256 = self._sha256

    @property
    def used_sha256(self) -> list[str]:
        return sorted(set(self._sha256.values()))

    @property
    def used_keys(self) -> list[str]:
        return sorted(self._sha256)


def kernels_for(
    channels: list[StationChannel],
    location: TrialLocation,
    cache: GreensCache,
    weights: dict[str, float],
) -> list[TraceKernel]:
    """One `TraceKernel` per channel, with the Green's functions rotated to this node."""
    out: list[TraceKernel] = []
    for channel in channels:
        distance_deg, azimuth_deg = geocentric_distance_azimuth(
            location.latitude, location.longitude, channel.latitude, channel.longitude
        )
        elementary = cache.elementary(distance_deg, location.depth_m)
        columns = rotate_to_zne(elementary, azimuth_deg)
        out.append(
            TraceKernel(
                key=channel.key,
                component=channel.component,
                data=channel.data,
                kernels=columns[channel.component],
                weight=weights.get(channel.key, 1.0),
                distance_deg=distance_deg,
                azimuth_deg=azimuth_deg,
            )
        )
    return out


@dataclass
class GridNodeResult:
    location: TrialLocation
    variance_reduction: float
    lambda_value: float
    azimuthal_gap_deg: float


@dataclass
class GridSearchResult:
    """What the search found, and enough of the surface to see how peaked it was."""

    best: GridNodeResult
    nodes: list[GridNodeResult]
    grid_spacing_km: float
    lambda_value: float
    stride: int
    n_basis: int
    shift: int
    greens_sha256: list[str]

    @property
    def variance_reduction(self) -> float:
        return self.best.variance_reduction

    def uncertainty_radius_km(self, drop: float = 0.02) -> float:
        """Radius containing every node within `drop` of the best variance reduction.

        This is a resolution statement about the grid search, not a formal confidence
        interval: it says how far the location can move before the fit visibly degrades.
        """
        threshold = self.best.variance_reduction - drop
        radii = [
            _distance_km(self.best.location, node.location)
            for node in self.nodes
            if node.variance_reduction >= threshold
        ]
        return float(max(radii)) if radii else 0.0

    def surface(self) -> list[dict[str, float]]:
        return [
            {
                "latitude": node.location.latitude,
                "longitude": node.location.longitude,
                "depth_m": node.location.depth_m,
                "variance_reduction": node.variance_reduction,
            }
            for node in self.nodes
        ]


def _distance_km(a: TrialLocation, b: TrialLocation) -> float:
    dlat = (b.latitude - a.latitude) * KM_PER_DEGREE
    dlon = (b.longitude - a.longitude) * KM_PER_DEGREE * math.cos(math.radians(a.latitude))
    return math.hypot(dlat, dlon)


def grid_search(
    channels: list[StationChannel],
    *,
    centre_lat: float,
    centre_lon: float,
    config: LfhConfig,
    cache: GreensCache,
    weights: dict[str, float],
    stride: int = 5,
    progress: Callable[[int], None] | None = None,
) -> GridSearchResult:
    """Invert at every trial location and keep the best variance reduction.

    Lambda is chosen once, by L-curve, at the nominal centre, and then held fixed across the
    grid. Re-choosing it per node would let the regularisation absorb location error and make
    the variance-reduction surface flatter than the data warrant.
    """
    n_fine = config.n_source_samples
    n_basis = max(math.ceil(n_fine / stride), 4)
    shift = config.greens_shift_samples

    centre = TrialLocation(centre_lat, centre_lon, config.grid.depths_m[0])
    normal = accumulate(
        kernels_for(channels, centre, cache, weights),
        n_basis=n_basis,
        stride=stride,
        shift=shift,
        dt=config.dt_s,
    )
    reg = config.regularisation
    seed = solve_normal(
        normal,
        stride=stride,
        zero_endpoints=reg.zero_endpoints,
        lambda_min=reg.lambda_min,
        lambda_max=reg.lambda_max,
        n_lambda=reg.n_lambda,
    )
    lambda_value = seed.lambda_value

    station_azimuths: dict[str, float] = {}
    nodes: list[GridNodeResult] = []
    for location in build_grid(centre_lat, centre_lon, config):
        kernels = kernels_for(channels, location, cache, weights)
        station_azimuths = {k.key.rsplit(".", 2)[0]: k.azimuth_deg for k in kernels}
        equations = accumulate(kernels, n_basis=n_basis, stride=stride, shift=shift, dt=config.dt_s)
        result = solve_normal(
            equations,
            stride=stride,
            zero_endpoints=reg.zero_endpoints,
            lambda_value=lambda_value,
        )
        nodes.append(
            GridNodeResult(
                location=location,
                variance_reduction=result.variance_reduction,
                lambda_value=lambda_value,
                azimuthal_gap_deg=azimuthal_gap(list(station_azimuths.values())),
            )
        )
        if progress is not None:
            progress(len(nodes))

    best = max(nodes, key=lambda node: node.variance_reduction)
    return GridSearchResult(
        best=best,
        nodes=nodes,
        grid_spacing_km=config.grid.spacing_km,
        lambda_value=lambda_value,
        stride=stride,
        n_basis=n_basis,
        shift=shift,
        greens_sha256=cache.used_sha256,
    )


def write_surface(result: GridSearchResult, path: Path) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.surface(), indent=2) + "\n", encoding="utf-8")
    return path
