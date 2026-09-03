"""Where the release volume is put, and what that placement cannot represent.

The Langtang source zone (85.51-85.53 E, 28.27-28.29 N, 3764-6144 m) sits about 4.5 km east
of the head of the committed OSM centreline, which starts at 4499 m. The detachment scar, the
free fall and the ice-rock fragmentation that precede entry into the Lhende Khola are **not**
in the model domain, and there is no mapped scar to put them on -- the AOI's source zone is a
rectangle chosen to contain the ComCat epicentre, not an observation of the failure surface.

So a member emplaces its whole release volume, **at rest**, on the corridor cells whose
elevation falls inside `release_elevation_band_m`, nearest the head of the corridor. Two
consequences are stated rather than buried:

* the roughly 1,300 m of drop between the detachment and the channel head contributes no
  kinetic energy, so modelled arrival times are biased **late** at every transect;
* the release elevation band is a *parameter* of the ensemble, not a measurement, and higher
  bands carry more potential energy purely because they start higher on the profile.

Both statements travel with the run as assumption strings.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from serac.models.runout.params import VoellmyParameters
from serac.models.runout.terrain import CorridorTerrain

F64 = NDArray[np.float64]
BOOL = NDArray[np.bool_]

RELEASE_AT_REST_ASSUMPTION = (
    "The release is emplaced at rest on the corridor cells inside the release elevation band. "
    "The detachment scar, the free fall from the Langtang Lirung flank and the fragmentation "
    "that precede entry into the Lhende Khola are outside the model domain, so roughly 1,300 m "
    "of drop contributes no initial kinetic energy and modelled arrival times are biased late."
)

RELEASE_BAND_ASSUMPTION = (
    "The release elevation band is an ensemble parameter, not a mapped failure surface: the "
    "AOI source zone is a rectangle chosen to contain the USGS ComCat epicentre of us7000tbwb."
)


@dataclass(frozen=True)
class Emplacement:
    """The initial depth field and what it took to build it."""

    depth: F64
    cells: int
    mean_depth_m: float
    band_low_m: float
    band_high_m: float
    chainage_max_m: float
    requested_volume_m3: float
    emplaced_volume_m3: float

    @property
    def shortfall_fraction(self) -> float:
        if self.requested_volume_m3 <= 0.0:
            return 0.0
        return 1.0 - self.emplaced_volume_m3 / self.requested_volume_m3


def emplace_release(
    terrain: CorridorTerrain,
    parameters: VoellmyParameters,
    *,
    head_length_m: float = 6000.0,
    max_depth_m: float = 300.0,
) -> Emplacement:
    """Spread `release_volume_m3` over the in-band cells within `head_length_m` of the head.

    Uniform depth over the selected cells. If the band selects too few cells the depth is
    capped at `max_depth_m` -- a 300 m deep column on a 30 m cell is already unphysical for a
    depth-averaged model -- and the band is widened until the volume fits or the head reach is
    exhausted. `Emplacement.shortfall_fraction` records any volume that could not be placed;
    the runner flags a member rather than silently shrinking its release.
    """
    low, high = parameters.release_elevation_band_m
    elevation = np.asarray(terrain.elevation, dtype=np.float64)
    chainage = np.asarray(terrain.chainage_m, dtype=np.float64)
    head = terrain.domain_mask & (chainage <= head_length_m)
    if not head.any():
        raise ValueError("no corridor cells within the head reach")

    cell_area = terrain.cell_area_m2
    band_low, band_high = low, high
    widen = 0.0
    selected = head & (elevation >= band_low) & (elevation <= band_high)
    needed = parameters.release_volume_m3 / max_depth_m / cell_area
    while selected.sum() < needed and widen < 3000.0:
        widen += 100.0
        band_low, band_high = low - widen, high + widen
        selected = head & (elevation >= band_low) & (elevation <= band_high)
    if not selected.any():
        # the band missed the corridor entirely: fall back to the highest cells in the head
        order = np.argsort(elevation[head])[::-1]
        take = max(1, int(needed))
        idx = np.flatnonzero(head.ravel())[order[:take]]
        selected = np.zeros_like(head).ravel()
        selected[idx] = True
        selected = selected.reshape(head.shape)
        band_low = float(elevation[selected].min())
        band_high = float(elevation[selected].max())

    n_cells = int(selected.sum())
    depth_value = min(parameters.release_volume_m3 / (n_cells * cell_area), max_depth_m)
    depth = np.zeros_like(elevation)
    depth[selected] = depth_value
    emplaced = float(depth.sum() * cell_area)
    return Emplacement(
        depth=depth,
        cells=n_cells,
        mean_depth_m=depth_value,
        band_low_m=float(band_low),
        band_high_m=float(band_high),
        chainage_max_m=float(chainage[selected].max()),
        requested_volume_m3=parameters.release_volume_m3,
        emplaced_volume_m3=emplaced,
    )
