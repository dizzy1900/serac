"""SAR viewing geometry and terrain geometry, in one place with the derivations written down.

Everything here is elementary vector geometry on a local ENU frame, but the sign conventions
are the sort of thing that is silently wrong for a whole session, so each one is stated and
each is covered by a test with a hand-checkable answer.

Conventions
-----------
* All azimuths are degrees clockwise from north.
* ENU means ``(east, north, up)`` with metres as the unit.
* ``aspect`` is the azimuth of steepest **descent** (the compass direction water runs), which
  is what GDAL/QGIS and this module both call aspect.
* ``heading`` is the azimuth of the satellite's ground track (its flight direction).
* Sentinel-1 is **right-looking**, so the horizontal look direction — from the satellite's
  ground track outwards towards the target, which is also the near-range-to-far-range
  direction — is ``heading + 90``.

Derivations
-----------
*Downslope unit vector.* Descending at slope ``theta`` along azimuth ``alpha``::

    d = (sin(alpha) cos(theta), cos(alpha) cos(theta), -sin(theta))

*Surface normal.* With ``z = f(E, N)``, the gradient points upslope with magnitude
``tan(theta)``, so ``grad = (-tan(theta) sin(alpha), -tan(theta) cos(alpha))`` and the
(unit, upward) normal is::

    n = (sin(theta) sin(alpha), sin(theta) cos(alpha), cos(theta))

i.e. the normal tilts towards the downslope azimuth, as it should: a north-facing slope has a
normal with a northward component.

*Line of sight.* Let ``i`` be the incidence angle and ``phi = heading + 90`` the horizontal
look azimuth. The satellite-to-ground look vector points along ``phi`` and downwards, so the
**ground-to-satellite** unit vector is::

    u = (-sin(i) sin(phi), -sin(i) cos(phi), cos(i))

Sanity check: an ascending pass has ``heading ~ 350``, so ``phi ~ 80`` and ``u`` has a large
negative east component — the satellite is to the west of the target, which is where an
ascending, right-looking satellite is. A descending pass (``heading ~ 190``, ``phi ~ 280``)
gives a positive east component. Both correct.

*Apparent dip.* The descent angle of the surface along a horizontal azimuth ``beta`` is the
apparent-dip formula ``delta = atan(tan(theta) cos(beta - alpha))``. Taking ``beta = phi``
(near range towards far range):

* ``delta > 0`` — the ground falls away from the sensor, so the slope faces the sensor and is
  foreshortened. **Layover** when ``delta >= i``.
* ``delta < 0`` — the ground rises away from the sensor (a back slope). **Shadow** when
  ``-delta >= 90 - i``.

Nominal incidence angles
------------------------
`IW_NOMINAL_INCIDENCE_DEG` holds mid-swath incidence angles for the three Sentinel-1 IW
subswaths. They are nominal design values used for *track selection only*, before any product
exists; once HyP3 delivers a pair, the per-pixel ``lv_theta`` raster is authoritative and is
what the time-series code uses. Track selection is a ranking, and a nominal mid-swath angle is
accurate to a couple of degrees, which does not reorder tracks — but the choice is disclosed
in the selection report all the same.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]

IW_NOMINAL_INCIDENCE_DEG: Final[dict[str, float]] = {
    "IW1": 32.9,
    "IW2": 38.3,
    "IW3": 43.1,
}
"""Nominal mid-swath incidence per Sentinel-1 IW subswath; see the module docstring."""

INCIDENCE_BASIS: Final[str] = (
    "nominal mid-swath Sentinel-1 IW incidence angle (IW1 32.9, IW2 38.3, IW3 43.1 degrees); "
    "used for track ranking only, superseded per pixel by the HyP3 lv_theta raster"
)


def _as_array(values: FloatArray | float) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def look_azimuth_deg(heading_deg: FloatArray | float) -> FloatArray:
    """Horizontal near-to-far look azimuth of a right-looking SAR: ``heading + 90``."""
    return np.mod(_as_array(heading_deg) + 90.0, 360.0)


def los_unit_vector(
    incidence_deg: FloatArray | float, heading_deg: FloatArray | float
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Ground-to-satellite unit vector in ENU. See the module docstring for the derivation."""
    inc = np.radians(_as_array(incidence_deg))
    phi = np.radians(look_azimuth_deg(heading_deg))
    return (
        -np.sin(inc) * np.sin(phi),
        -np.sin(inc) * np.cos(phi),
        np.cos(inc) * np.ones_like(phi),
    )


def downslope_unit_vector(
    slope_deg: FloatArray | float, aspect_deg: FloatArray | float
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Unit vector pointing down the slope, in ENU."""
    theta = np.radians(_as_array(slope_deg))
    alpha = np.radians(_as_array(aspect_deg))
    return (
        np.sin(alpha) * np.cos(theta),
        np.cos(alpha) * np.cos(theta),
        -np.sin(theta) * np.ones_like(alpha),
    )


def surface_normal(
    slope_deg: FloatArray | float, aspect_deg: FloatArray | float
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Upward unit normal of the surface, in ENU."""
    theta = np.radians(_as_array(slope_deg))
    alpha = np.radians(_as_array(aspect_deg))
    return (
        np.sin(theta) * np.sin(alpha),
        np.sin(theta) * np.cos(alpha),
        np.cos(theta) * np.ones_like(alpha),
    )


def los_sensitivity(
    slope_deg: FloatArray | float,
    aspect_deg: FloatArray | float,
    incidence_deg: FloatArray | float,
    heading_deg: FloatArray | float,
) -> FloatArray:
    """``|d . u|`` — the fraction of downslope motion a track actually measures.

    1.0 would mean the slope moves straight along the line of sight; 0.0 means the motion is
    entirely perpendicular to it and the track is blind to it however large it is.
    """
    de, dn, du = downslope_unit_vector(slope_deg, aspect_deg)
    le, ln, lu = los_unit_vector(incidence_deg, heading_deg)
    return np.abs(de * le + dn * ln + du * lu)


def local_incidence_deg(
    slope_deg: FloatArray | float,
    aspect_deg: FloatArray | float,
    incidence_deg: FloatArray | float,
    heading_deg: FloatArray | float,
) -> FloatArray:
    """Angle between the surface normal and the line of sight, in degrees."""
    ne, nn, nu = surface_normal(slope_deg, aspect_deg)
    le, ln, lu = los_unit_vector(incidence_deg, heading_deg)
    dot = np.clip(ne * le + nn * ln + nu * lu, -1.0, 1.0)
    return np.degrees(np.arccos(dot))


def apparent_dip_deg(
    slope_deg: FloatArray | float, aspect_deg: FloatArray | float, azimuth_deg: FloatArray | float
) -> FloatArray:
    """Descent angle of the surface along a horizontal azimuth (positive means falling away)."""
    theta = np.radians(_as_array(slope_deg))
    alpha = np.radians(_as_array(aspect_deg))
    beta = np.radians(_as_array(azimuth_deg))
    return np.degrees(np.arctan(np.tan(theta) * np.cos(beta - alpha)))


def layover_shadow_masks(
    slope_deg: FloatArray,
    aspect_deg: FloatArray,
    incidence_deg: float,
    heading_deg: float,
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """``(layover, shadow)`` boolean masks from the apparent-dip test in the module docstring.

    This is a *local-slope* geometric test, not a ray-traced simulation: it finds the pixels
    that lay over or are shadowed **by their own slope**, not those hidden behind a distant
    ridge. It therefore under-counts, which is stated wherever the fraction is reported.
    """
    dip = apparent_dip_deg(slope_deg, aspect_deg, look_azimuth_deg(heading_deg))
    layover = dip >= incidence_deg
    shadow = -dip >= (90.0 - incidence_deg)
    return np.asarray(layover, dtype=np.bool_), np.asarray(shadow, dtype=np.bool_)


def slope_aspect(
    dem: FloatArray, pixel_x_m: float, pixel_y_m: float
) -> tuple[FloatArray, FloatArray]:
    """Horn (1981) third-order finite-difference slope and aspect from a projected DEM.

    `dem` is row-major with row 0 at the **top** (north), which is how rasterio hands over a
    north-up raster, so the north gradient carries a sign flip. Edge pixels use replicated
    borders. Returns ``(slope_deg in [0, 90], aspect_deg in [0, 360))``; aspect of a perfectly
    flat pixel is defined as 0.0 and such pixels are excluded by every downstream slope mask.
    """
    if pixel_x_m <= 0 or pixel_y_m <= 0:
        raise ValueError("pixel sizes must be positive metres")
    if dem.ndim != 2 or min(dem.shape) < 3:
        raise ValueError("dem must be a 2-D array at least 3x3")
    z = np.pad(np.asarray(dem, dtype=np.float64), 1, mode="edge")
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, _e, f = z[1:-1, :-2], z[1:-1, 1:-1], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dz_de = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * pixel_x_m)
    dz_dn = ((a + 2 * b + c) - (g + 2 * h + i)) / (8.0 * pixel_y_m)
    slope = np.degrees(np.arctan(np.hypot(dz_de, dz_dn)))
    # Gradient points upslope; aspect is the descent azimuth, hence the negated components.
    aspect = np.degrees(np.arctan2(-dz_de, -dz_dn))
    aspect = np.mod(aspect, 360.0)
    aspect = np.where(np.hypot(dz_de, dz_dn) < 1e-12, 0.0, aspect)
    return slope, aspect


def heading_from_footprint(coordinates: list[tuple[float, float]], flight_direction: str) -> float:
    """Satellite heading in degrees from a burst/scene footprint polygon.

    A Sentinel-1 IW burst footprint is a quadrilateral roughly 80 km across track by 20 km
    along track. The **shorter** pair of opposite edges runs along track, so the azimuth of
    the shortest edge is the heading up to a 180-degree ambiguity, which `flight_direction`
    resolves: an ascending pass heads roughly north, a descending pass roughly south.

    Deriving the heading from the delivered geometry rather than hard-coding a remembered
    orbital constant means the number is provenanced by the search response.
    """
    pts = [(float(x), float(y)) for x, y in coordinates]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        raise ValueError("footprint needs at least 3 distinct vertices")
    mean_lat = sum(p[1] for p in pts) / len(pts)
    scale = math.cos(math.radians(mean_lat))
    best: tuple[float, float] | None = None
    for j, (lon0, lat0) in enumerate(pts):
        lon1, lat1 = pts[(j + 1) % len(pts)]
        de = (lon1 - lon0) * scale
        dn = lat1 - lat0
        length = math.hypot(de, dn)
        if length <= 0:
            continue
        if best is None or length < best[0]:
            best = (length, math.degrees(math.atan2(de, dn)) % 360.0)
    if best is None:
        raise ValueError("footprint has no edge with non-zero length")
    heading = best[1]
    ascending = flight_direction.upper().startswith("ASC")
    northward = heading < 90.0 or heading > 270.0
    if ascending != northward:
        heading = (heading + 180.0) % 360.0
    return heading
