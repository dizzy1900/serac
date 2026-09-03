"""The corridor frame: a chainage/offset coordinate system built on the AOI centreline.

Why the solver does **not** run in this frame
---------------------------------------------
The obvious design is to resample the DEM into an orthogonal `(s, n)` ribbon and solve there,
because a 100 km x 1.2 km ribbon is ~14x smaller than the map-space bounding box of the same
corridor. Measured on the committed Lhende centreline that does not work: the OSM river
geometry is genuinely sinuous, and the minimum radius of curvature is 750 m even after a 900 m
Gaussian smooth (which by then has moved the line up to 574 m off the real channel). An
orthogonal ribbon of half-width `w` folds wherever `w > R`, and a curvilinear solver would need
metric source terms whose correctness cannot be checked by the Cartesian verification tests
(Ritter, lake-at-rest, terminal velocity) that this module's solver is held to.

So the solver runs in map space (`serac.models.runout.solver`) and this module supplies:

* `inverse` — map `(x, y) -> (s, n)`, the nearest point on the polyline. Single-valued and
  robust everywhere: no fold, no smoothing needed. This is what reduces a 2-D depth field onto
  the 1-D chainage profiles the surrogate learns.
* `forward` — `(s, n) -> (x, y)`, `P(s) + n * N_segment(s)` about the **raw** resampled
  centreline. Used for rendering and for the round-trip test.

Round trip, measured not assumed
--------------------------------
`forward(inverse(p)) == p` to **1.6e-11 px** (machine precision) on every cell whose closest-
point projection falls strictly inside a segment, and it is wrong -- by up to 71 px -- on every
cell whose projection snaps to a *vertex*. That second set is not a bug to be tuned away: the
domain mask is a 1.5 km **buffer** of the centreline, so its rounded end-caps and the outer
side of every bend lie beyond the curve's medial axis, where `(x, y) -> (s, n)` is genuinely
many-to-one and no forward map can invert it. On the committed Lhende corridor at 30 m it is
61% of the mask.

So the frame publishes `projection_interior(s)`, `CorridorTerrain.frame_valid` is the mask the
round-trip is gated on, and `roundtrip_rms_px` reports both sets separately. Smoothing the
centreline was tried and rejected: it left the RMS at 5-7 px and moved the recomputed transect
chainages up to 8.2 km away from the committed ones.

Conventions: `s` is chainage in metres from the head of the centreline, increasing downstream;
`n` is signed offset in metres, positive to the left of the direction of travel.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from pyproj import Transformer
from shapely import STRtree
from shapely import points as shapely_points

CENTRELINE_FILENAME = "river_centreline.geojson"
TRANSECTS_FILENAME = "transects.geojson"
DEFAULT_SAMPLE_SPACING_M = 30.0
DEFAULT_NORMAL_SMOOTH_M = 300.0
"""Gaussian sigma applied to the tangent direction only; the anchor points stay raw."""

F64 = NDArray[np.float64]
BOOL = NDArray[np.bool_]


def _gaussian_smooth(values: F64, sigma_samples: float) -> F64:
    """Edge-padded Gaussian smooth along the first axis."""
    if sigma_samples <= 0:
        return values.copy()
    radius = max(1, math.ceil(4.0 * sigma_samples))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (offsets / sigma_samples) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values, radius, mode="edge")
    out: F64 = np.convolve(padded, kernel, mode="same")[radius:-radius]
    return out


def read_centreline_lonlat(path: Path) -> F64:
    """The `(lon, lat)` vertices of the single LineString in an AOI centreline GeoJSON."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    features = doc["features"] if doc.get("type") == "FeatureCollection" else [doc]
    lines: list[list[list[float]]] = []
    for feature in features:
        geom = feature.get("geometry", feature)
        if geom["type"] == "LineString":
            lines.append(geom["coordinates"])
        elif geom["type"] == "MultiLineString":
            lines.extend(geom["coordinates"])
    if len(lines) != 1:
        raise ValueError(f"{path}: expected exactly one LineString, found {len(lines)}")
    return np.asarray(lines[0], dtype=np.float64)


@dataclass(frozen=True)
class CorridorFrame:
    """A chainage/offset frame anchored on a centreline resampled at `spacing_m`.

    `points` are the resampled centreline vertices in `epsg` (metres); `chainage` is their
    arc length; `tangent`/`normal` are unit vectors from the smoothed heading.
    """

    epsg: int
    spacing_m: float
    half_width_m: float
    points: F64
    chainage: F64
    tangent: F64
    normal: F64
    curvature: F64
    smooth_sigma_m: float

    @property
    def length_m(self) -> float:
        return float(self.chainage[-1])

    @property
    def n_samples(self) -> int:
        return int(self.points.shape[0])

    @cached_property
    def segment_normal(self) -> F64:
        """Unit normal of each polyline *segment*, left of the direction of travel.

        `forward` uses these, not the smoothed `normal`, so that it is the exact inverse of
        `inverse`'s point-to-segment projection. Using the smoothed normal here instead cost
        about 223 m of round-trip error on this centreline at offsets of 1.5 km, because the
        smoothed heading is not the direction back to the point.
        """
        seg = self.points[1:] - self.points[:-1]
        length = np.maximum(np.linalg.norm(seg, axis=1, keepdims=True), 1e-12)
        unit = seg / length
        return np.column_stack([-unit[:, 1], unit[:, 0]])

    def forward(self, s: F64, n: F64) -> tuple[F64, F64]:
        """`(s, n) -> (x, y)`: walk `s` along the polyline, then `n` along that segment's normal.

        Exactly inverts `inverse` for any point whose projection falls inside the polyline.
        """
        s = np.asarray(s, dtype=np.float64)
        n = np.asarray(n, dtype=np.float64)
        idx = np.clip(s / self.spacing_m, 0.0, self.n_samples - 1.000001)
        lo = np.floor(idx).astype(np.int64)
        seg_index = np.clip(lo, 0, self.n_samples - 2)
        t = (idx - seg_index)[..., None]
        base = self.points[seg_index] * (1.0 - t) + self.points[seg_index + 1] * t
        out = base + self.segment_normal[seg_index] * n[..., None]
        return out[..., 0], out[..., 1]

    @cached_property
    def _vertex_tree(self) -> STRtree:
        """Spatial index over the resampled vertices; the entry point for `inverse`."""
        vertices = np.asarray(shapely_points(self.points[:, 0], self.points[:, 1]))
        return STRtree(vertices)

    def _project(self, xf: F64, yf: F64, starts: NDArray[np.int64]) -> tuple[F64, F64, F64]:
        """Project points onto the segments beginning at `starts`: `(s, signed n, distance^2)`."""
        a = self.points[starts]
        seg = self.points[starts + 1] - a
        seg_len2 = np.maximum((seg * seg).sum(axis=-1), 1e-12)
        px = xf - a[..., 0]
        py = yf - a[..., 1]
        t = np.clip((px * seg[..., 0] + py * seg[..., 1]) / seg_len2, 0.0, 1.0)
        dx = px - t * seg[..., 0]
        dy = py - t * seg[..., 1]
        d2 = dx * dx + dy * dy
        s = self.chainage[starts] + t * self.spacing_m
        # sign: positive to the left of the direction of travel (2-D cross product)
        cross = seg[..., 0] * dy - seg[..., 1] * dx
        return s, np.sign(cross) * np.sqrt(d2), d2

    def inverse(
        self, x: F64, y: F64, *, chunk: int = 400_000, neighbourhood: int = 2
    ) -> tuple[F64, F64]:
        """`(x, y) -> (s, n)`: the nearest point on the resampled polyline.

        The nearest *vertex* comes from an STRtree; the answer is then refined by projecting
        onto the `2 * neighbourhood` segments around it, so the result is a true point-to-
        segment projection rather than a vertex snap. `inverse_brute` is the reference
        implementation and `tests/unit/models/test_runout_corridor.py` asserts they agree.
        """
        shape = np.asarray(x).shape
        xf = np.asarray(x, dtype=np.float64).ravel()
        yf = np.asarray(y, dtype=np.float64).ravel()
        last = self.n_samples - 2
        offsets = np.arange(-neighbourhood, neighbourhood, dtype=np.int64)
        s_out = np.empty(xf.size, dtype=np.float64)
        n_out = np.empty(xf.size, dtype=np.float64)
        for start in range(0, xf.size, chunk):
            stop = min(start + chunk, xf.size)
            qx, qy = xf[start:stop], yf[start:stop]
            # shapely returns (2, n) for array input: row 0 input index, row 1 tree index
            raw = np.asarray(
                self._vertex_tree.query_nearest(shapely_points(qx, qy), all_matches=False),
                dtype=np.int64,
            )
            nearest = raw[1] if raw.ndim == 2 else raw
            starts = np.clip(nearest[:, None] + offsets[None, :], 0, last)
            s, n, d2 = self._project(qx[:, None], qy[:, None], starts)
            best = np.argmin(d2, axis=1)
            rows = np.arange(stop - start)
            s_out[start:stop] = s[rows, best]
            n_out[start:stop] = n[rows, best]
        return s_out.reshape(shape), n_out.reshape(shape)

    def projection_interior(self, s: F64, tol: float = 1e-9) -> BOOL:
        """Where the closest-point projection landed strictly *inside* a segment.

        `forward` inverts `inverse` exactly (measured: 1.6e-11 px) on this set and nowhere else.
        The complement is where the projection snapped to a vertex -- the rounded end-caps of the
        1.5 km corridor buffer and the outer side of every bend -- and there the map `(x, y) ->
        (s, n)` is genuinely many-to-one, so no forward map can invert it. On the committed
        Lhende corridor that complement is 61% of the 30 m domain mask. Chainage binning, which
        is all the surrogate and the reports use the frame for, only needs `s` and is unaffected;
        anything that needs to travel back to map coordinates must respect this mask.
        """
        idx = np.asarray(s, dtype=np.float64) / self.spacing_m
        seg = np.floor(np.clip(idx, 0.0, self.n_samples - 1.000001))
        frac = idx - seg
        return (frac > tol) & (frac < 1.0 - tol)

    def inverse_brute(self, x: F64, y: F64) -> tuple[F64, F64]:
        """Reference `inverse`: projects onto every segment. O(points x segments); tests only."""
        shape = np.asarray(x).shape
        xf = np.asarray(x, dtype=np.float64).ravel()
        yf = np.asarray(y, dtype=np.float64).ravel()
        starts = np.arange(self.n_samples - 1, dtype=np.int64)
        s, n, d2 = self._project(
            xf[:, None], yf[:, None], np.broadcast_to(starts, (xf.size, *starts.shape))
        )
        best = np.argmin(d2, axis=1)
        rows = np.arange(xf.size)
        return s[rows, best].reshape(shape), n[rows, best].reshape(shape)

    def max_area_distortion(self) -> float:
        """`max |n * kappa|` over the ribbon: how far from area-preserving `forward` is."""
        return float(np.max(np.abs(self.curvature)) * self.half_width_m)


def build_frame(
    lonlat: F64,
    epsg: int,
    *,
    spacing_m: float = DEFAULT_SAMPLE_SPACING_M,
    half_width_m: float = 1500.0,
    smooth_sigma_m: float = DEFAULT_NORMAL_SMOOTH_M,
) -> CorridorFrame:
    """Project, resample at `spacing_m` and derive smoothed tangents/normals and curvature."""
    if spacing_m <= 0:
        raise ValueError("spacing_m must be > 0")
    if half_width_m <= 0:
        raise ValueError("half_width_m must be > 0")
    transformer = Transformer.from_crs(4326, epsg, always_xy=True)
    px, py = transformer.transform(lonlat[:, 0], lonlat[:, 1])
    raw = np.column_stack([np.asarray(px, dtype=np.float64), np.asarray(py, dtype=np.float64)])
    steps = np.hypot(*np.diff(raw, axis=0).T)
    arc = np.concatenate([[0.0], np.cumsum(steps)])
    if arc[-1] <= spacing_m:
        raise ValueError(f"centreline is only {arc[-1]:.1f} m long")
    chainage = np.arange(0.0, arc[-1] + 0.5 * spacing_m, spacing_m, dtype=np.float64)
    points = np.column_stack(
        [np.interp(chainage, arc, raw[:, 0]), np.interp(chainage, arc, raw[:, 1])]
    )

    sigma_samples = smooth_sigma_m / spacing_m
    sx = _gaussian_smooth(points[:, 0], sigma_samples)
    sy = _gaussian_smooth(points[:, 1], sigma_samples)
    dx = np.gradient(sx, spacing_m)
    dy = np.gradient(sy, spacing_m)
    speed = np.maximum(np.hypot(dx, dy), 1e-12)
    tangent = np.column_stack([dx / speed, dy / speed])
    normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])
    ddx = np.gradient(dx, spacing_m)
    ddy = np.gradient(dy, spacing_m)
    curvature = (dx * ddy - dy * ddx) / np.maximum(speed**3, 1e-12)
    return CorridorFrame(
        epsg=epsg,
        spacing_m=spacing_m,
        half_width_m=half_width_m,
        points=points,
        chainage=chainage,
        tangent=tangent,
        normal=normal,
        curvature=curvature,
        smooth_sigma_m=smooth_sigma_m,
    )


def load_frame(
    aoi_dir: Path,
    epsg: int,
    *,
    spacing_m: float = DEFAULT_SAMPLE_SPACING_M,
    half_width_m: float = 1500.0,
    smooth_sigma_m: float = DEFAULT_NORMAL_SMOOTH_M,
) -> CorridorFrame:
    """Build the frame from `data/aoi/<id>/river_centreline.geojson`."""
    lonlat = read_centreline_lonlat(aoi_dir / CENTRELINE_FILENAME)
    return build_frame(
        lonlat,
        epsg,
        spacing_m=spacing_m,
        half_width_m=half_width_m,
        smooth_sigma_m=smooth_sigma_m,
    )


def roundtrip_rms_px(
    frame: CorridorFrame, x: F64, y: F64, resolution_m: float
) -> tuple[float, float]:
    """`(rms, max)` of `|forward(inverse(p)) - p|` in pixels, over the points given."""
    s, n = frame.inverse(x, y)
    bx, by = frame.forward(s, n)
    err = np.hypot(bx - np.asarray(x), by - np.asarray(y)) / resolution_m
    return float(np.sqrt(np.mean(err**2))), float(np.max(err))


@dataclass(frozen=True)
class TransectChainage:
    """A committed transect placed on the frame."""

    transect_id: str
    declared_chainage_km: float
    frame_chainage_m: float
    offset_m: float


def transect_chainages(aoi_dir: Path, frame: CorridorFrame) -> list[TransectChainage]:
    """Project the AOI's committed transect points onto the frame, keeping both chainages.

    The declared `chainage_km` comes from the AOI build; the frame chainage is recomputed here.
    They are reported side by side rather than reconciled, so a disagreement stays visible.
    """
    doc = json.loads((aoi_dir / TRANSECTS_FILENAME).read_text(encoding="utf-8"))
    transformer = Transformer.from_crs(4326, frame.epsg, always_xy=True)
    out: list[TransectChainage] = []
    for feature in doc["features"]:
        lon, lat = feature["geometry"]["coordinates"][:2]
        px, py = transformer.transform(lon, lat)
        s, n = frame.inverse(np.asarray([px]), np.asarray([py]))
        out.append(
            TransectChainage(
                transect_id=str(feature["properties"]["id"]),
                declared_chainage_km=float(feature["properties"]["chainage_km"]),
                frame_chainage_m=float(s[0]),
                offset_m=float(n[0]),
            )
        )
    return sorted(out, key=lambda t: t.frame_chainage_m)
