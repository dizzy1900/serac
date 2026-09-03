"""From a force history to a centre-of-mass trajectory, and from the terrain back to a mass.

A single force is Newton's third law made visible: the force the slide exerts on the Earth is
the reaction to the force the Earth exerts on the slide, so the slide's acceleration is
`a(t) = -F(t) / M`. Integrating twice gives the centre-of-mass path -- but only up to the
unknown mass, because every displacement scales as `1 / M`.

That is the opening the DEM provides. The *shape* and *direction* of the path come from the
seismology alone; the mass is the one number that sets its size. Laying the unit-mass path on
the real terrain from the inverted source location and asking which mass makes the modelled
vertical drop match the ground's own drop over the corresponding horizontal distance closes
the system:

    find M such that   Dz(1 kg) / M  ==  z(source) - z(source + Dh(1 kg) / M along azimuth)

One equation, one unknown, solved by bisection. It is a real use of the terrain rather than a
slope guessed from a table, and it fails loudly when the DEM does not cover the runout.

Where no DEM crop exists, `slope_from_drop_and_runout` takes a published fall height and
runout instead. That is a weaker input and it is labelled as such: the resulting `AEff` gets
`basis="assumed_range"`, never `"dem_trajectory"`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = 2.0 * math.pi * EARTH_RADIUS_KM / 360.0


@dataclass(frozen=True)
class Trajectory:
    """The centre-of-mass path implied by a force history and a mass."""

    time_s: np.ndarray
    acceleration_m_s2: np.ndarray
    velocity_m_s: np.ndarray
    displacement_m: np.ndarray
    mass_kg: float

    @property
    def peak_speed_m_s(self) -> float:
        return float(np.linalg.norm(self.velocity_m_s, axis=0).max())

    @property
    def horizontal_runout_m(self) -> float:
        horizontal = self.displacement_m[1:, :]
        return float(np.linalg.norm(horizontal[:, -1]))

    @property
    def drop_m(self) -> float:
        """Downward displacement of the centre of mass, positive when it fell."""
        return float(-self.displacement_m[0, -1])

    @property
    def path_length_m(self) -> float:
        steps = np.diff(self.displacement_m, axis=1)
        return float(np.linalg.norm(steps, axis=0).sum())

    @property
    def azimuth_deg(self) -> float:
        north, east = self.displacement_m[1, -1], self.displacement_m[2, -1]
        return float(math.degrees(math.atan2(east, north)) % 360.0)

    @property
    def path_angle_deg(self) -> float:
        runout = self.horizontal_runout_m
        if runout <= 0:
            return 90.0
        return float(math.degrees(math.atan2(max(self.drop_m, 0.0), runout)))


def integrate(forces_n: np.ndarray, *, dt: float, mass_kg: float) -> Trajectory:
    """`a = -F/M`, then cumulative trapezoidal integration twice.

    `forces_n` is `(3, n)` in the (up, north, east) frame. The sign is the point: a slide
    accelerating downslope pushes the Earth upslope, so the force serac inverts for points
    the other way from the motion.
    """
    if forces_n.ndim != 2 or forces_n.shape[0] != 3:
        raise ValueError(f"forces must be (3, n); got {forces_n.shape}")
    if mass_kg <= 0:
        raise ValueError("mass must be positive")
    n = forces_n.shape[1]
    time = np.arange(n) * dt
    acceleration = -forces_n / mass_kg
    velocity = _cumtrapz(acceleration, dt)
    displacement = _cumtrapz(velocity, dt)
    return Trajectory(
        time_s=time,
        acceleration_m_s2=acceleration,
        velocity_m_s=velocity,
        displacement_m=displacement,
        mass_kg=mass_kg,
    )


def _cumtrapz(values: np.ndarray, dt: float) -> np.ndarray:
    out = np.zeros_like(values)
    if values.shape[1] < 2:
        return out
    increments = 0.5 * dt * (values[:, 1:] + values[:, :-1])
    out[:, 1:] = np.cumsum(increments, axis=1)
    return out


def unit_mass_displacement(forces_n: np.ndarray, *, dt: float) -> np.ndarray:
    """`(3,)` final displacement for a one-kilogram mass; the real one is this over `M`."""
    return integrate(forces_n, dt=dt, mass_kg=1.0).displacement_m[:, -1]


def path_angle_from_force(forces_n: np.ndarray) -> float:
    """Effective path inclination read from the force history alone, in degrees.

    On a slope of angle `theta` the reaction force has vertical part `M a sin(theta)` and
    horizontal part `M a cos(theta)`, so `theta = atan(|F_vertical| / |F_horizontal|)`. Taking
    the ratio at the instant of peak horizontal force uses the acceleration phase, where the
    slope approximation is best.

    This is what makes the second mass estimator seismically self-contained: no DEM, no
    catalogue, no published geometry -- only the inverted force.
    """
    horizontal = np.linalg.norm(forces_n[1:, :], axis=0)
    if horizontal.size == 0 or float(horizontal.max()) <= 0:
        return 90.0
    index = int(np.argmax(horizontal))
    return float(math.degrees(math.atan2(abs(float(forces_n[0, index])), float(horizontal[index]))))


def runout_azimuth(forces_n: np.ndarray, *, dt: float) -> float:
    """Bearing the centre of mass travelled, in degrees from north.

    This is **not** the force azimuth. The slide accelerates as `a = -F/M`, so the direction
    it moved is opposite the direction of the force it exerted on the ground. Sampling a DEM
    profile along the force azimuth points it up the mountain instead of down it, and the
    terrain solve then correctly reports that no mass reconciles the two -- an error that
    presents as a failed estimator rather than a wrong number, but an error nonetheless.

    The bearing is taken from the net displacement of a unit-mass integration, which is
    independent of the mass because every displacement scales as 1/M.
    """
    displacement = unit_mass_displacement(forces_n, dt=dt)
    return float(math.degrees(math.atan2(displacement[2], displacement[1])) % 360.0)


def slope_from_drop_and_runout(drop_m: float, runout_m: float) -> float:
    """`atan(H / L)` in degrees: the coarse fallback when there is no DEM."""
    if runout_m <= 0:
        return 90.0
    return float(math.degrees(math.atan2(max(drop_m, 0.0), runout_m)))


@dataclass(frozen=True)
class TerrainProfile:
    """Elevation along a bearing from the source: what the DEM actually says."""

    distance_m: np.ndarray
    elevation_m: np.ndarray
    azimuth_deg: float
    source_elevation_m: float
    dem_path: str
    n_valid: int

    def elevation_at(self, distance_m: float) -> float:
        if self.distance_m.size == 0:
            raise ValueError("empty terrain profile")
        clamped = float(np.clip(distance_m, self.distance_m[0], self.distance_m[-1]))
        return float(np.interp(clamped, self.distance_m, self.elevation_m))

    def drop_at(self, distance_m: float) -> float:
        return self.source_elevation_m - self.elevation_at(distance_m)

    @property
    def max_distance_m(self) -> float:
        return float(self.distance_m[-1]) if self.distance_m.size else 0.0


class TerrainUnavailableError(Exception):
    """No DEM covers the trajectory, so the DEM-trajectory estimator must not be reported."""


def read_terrain_profile(
    dem_path: Path,
    *,
    source_lat: float,
    source_lon: float,
    azimuth_deg: float,
    max_distance_m: float,
    step_m: float = 30.0,
) -> TerrainProfile:
    """Sample a GeoTIFF along a bearing from the source.

    Nothing is extrapolated: samples that fall outside the raster or on nodata are dropped and
    counted, and the caller sees how far the profile actually reaches.
    """
    import rasterio
    from rasterio.errors import RasterioError

    n = max(int(max_distance_m / step_m) + 1, 2)
    distances = np.arange(n) * step_m
    bearing = math.radians(azimuth_deg)
    dlat = distances * math.cos(bearing) / (KM_PER_DEGREE * 1000.0)
    cos_lat = max(math.cos(math.radians(source_lat)), 1e-6)
    dlon = distances * math.sin(bearing) / (KM_PER_DEGREE * 1000.0 * cos_lat)
    lats = source_lat + dlat
    lons = source_lon + dlon

    try:
        with rasterio.open(dem_path) as dataset:
            nodata = dataset.nodata
            samples = np.array(
                [value[0] for value in dataset.sample(zip(lons, lats, strict=True))], dtype=float
            )
            bounds = dataset.bounds
    except (RasterioError, OSError) as exc:
        raise TerrainUnavailableError(f"cannot read DEM {dem_path}: {exc}") from exc

    inside = (
        (lons >= bounds.left)
        & (lons <= bounds.right)
        & (lats >= bounds.bottom)
        & (lats <= bounds.top)
    )
    valid = inside & np.isfinite(samples)
    if nodata is not None:
        valid &= samples != nodata
    valid &= samples > -1000.0
    if not valid.any():
        raise TerrainUnavailableError(
            f"DEM {dem_path.name} covers none of the profile from "
            f"({source_lat:.4f}, {source_lon:.4f}) on bearing {azimuth_deg:.0f} deg"
        )
    # Keep the leading run of valid samples: a profile that leaves the crop and comes back is
    # not a profile, and interpolating across the hole would invent terrain.
    first_invalid = int(np.argmin(valid)) if not valid.all() else valid.size
    keep = slice(0, max(first_invalid, 2))
    kept_distance = distances[keep]
    kept_elevation = samples[keep]
    if kept_elevation.size < 2 or not np.isfinite(kept_elevation).all():
        raise TerrainUnavailableError(
            f"DEM {dem_path.name} gives fewer than two valid samples along the trajectory"
        )
    return TerrainProfile(
        distance_m=kept_distance,
        elevation_m=kept_elevation,
        azimuth_deg=azimuth_deg,
        source_elevation_m=float(kept_elevation[0]),
        dem_path=dem_path.as_posix(),
        n_valid=int(valid.sum()),
    )


@dataclass(frozen=True)
class TerrainMassSolution:
    """The mass at which the seismic trajectory and the terrain agree."""

    mass_kg: float
    modelled_drop_m: float
    terrain_drop_m: float
    horizontal_runout_m: float
    path_angle_deg: float
    bracket_kg: tuple[float, float]
    converged: bool


def mass_from_terrain(
    forces_n: np.ndarray,
    profile: TerrainProfile,
    *,
    dt: float,
    mass_min_kg: float = 1e7,
    mass_max_kg: float = 1e13,
    tolerance: float = 1e-3,
    max_iterations: int = 200,
) -> TerrainMassSolution:
    """Solve `Dz(1 kg)/M - terrain_drop(Dh(1 kg)/M) = 0` for `M` by bisection.

    Both sides fall as the mass rises -- a heavier slide moves less far and drops less -- but
    at different rates, which is what makes the crossing well defined. A sign change is
    required over the bracket; without one the terrain and the force history are inconsistent
    and the estimator says so rather than returning an endpoint.
    """
    unit = unit_mass_displacement(forces_n, dt=dt)
    drop_unit = float(-unit[0])
    runout_unit = float(np.linalg.norm(unit[1:]))
    if runout_unit <= 0 or drop_unit <= 0:
        raise TerrainUnavailableError(
            "the inverted trajectory neither descends nor travels; no terrain solution exists"
        )

    def residual(mass: float) -> float:
        modelled_drop = drop_unit / mass
        runout = runout_unit / mass
        if runout > profile.max_distance_m:
            # Beyond the crop the terrain is unknown; treat it as the far edge, which biases
            # towards *smaller* drops and therefore towards larger masses, the conservative way.
            runout = profile.max_distance_m
        return modelled_drop - profile.drop_at(runout)

    lo, hi = mass_min_kg, mass_max_kg
    f_lo, f_hi = residual(lo), residual(hi)
    if f_lo * f_hi > 0:
        raise TerrainUnavailableError(
            f"no mass in [{mass_min_kg:.1e}, {mass_max_kg:.1e}] kg reconciles the seismic "
            f"trajectory with the DEM profile (residuals {f_lo:.1f} and {f_hi:.1f} m)"
        )
    converged = False
    mid = math.sqrt(lo * hi)
    for _ in range(max_iterations):
        mid = math.sqrt(lo * hi)
        value = residual(mid)
        if abs(value) < tolerance * max(abs(drop_unit / mid), 1.0):
            converged = True
            break
        if value * f_lo > 0:
            lo, f_lo = mid, value
        else:
            hi = mid
    runout = min(runout_unit / mid, profile.max_distance_m)
    return TerrainMassSolution(
        mass_kg=mid,
        modelled_drop_m=drop_unit / mid,
        terrain_drop_m=profile.drop_at(runout),
        horizontal_runout_m=runout,
        path_angle_deg=slope_from_drop_and_runout(drop_unit / mid, runout),
        bracket_kg=(mass_min_kg, mass_max_kg),
        converged=converged,
    )
