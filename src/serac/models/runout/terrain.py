"""Corridor terrain: reproject the GLO-30 crop, condition it, and derive the solver's layers.

The committed `data/fixtures/dem_glo30/lhende-khola-trishuli/glo30_crop.tif` covers only the
source zone (203 x 219 px). The corridor DEM is fetched separately into `data/raw/` by
`serac runout terrain --fetch` (or `serac ingest dem` with the AOI bbox) and ledgered.

Pipeline
--------
1. **Reproject** the EPSG:4326 float32 crop onto the AOI's UTM grid (EPSG:32645) at the
   requested resolution, bilinear. The grid is the committed `data/aoi/<id>/grid.json` cropped
   to the corridor mask's bounding box plus a margin, so the raster and the AOI cube stay
   pixel-aligned at 30 m and coarser runs are exact integer multiples of it.
2. **Domain mask** from the committed `corridor.geojson` (a 1.5 km buffer of the OSM
   centreline). Cells outside are solid wall.
3. **Condition**: priority-flood depression filling (Barnes, Lehman & Mulla 2014) with an
   epsilon gradient, seeded *only* from the downstream outflow edge of the mask. Because that
   is the single open boundary, filling to it guarantees every masked cell has a strictly
   descending path to the outlet -- which is exactly "fill pits, enforce drainage". The
   monotonicity of the thalweg is then a property to assert, not a second carving step.
4. **Erodible depth**: a *parametric* mantle, not an observation. There is no measured
   sediment-thickness dataset for this corridor, so the layer is a stated closure --
   `d_max * f_slope * f_offset` with a slope cut-off and a Gaussian taper away from the
   thalweg. Every forecast built on it carries that assumption string; it is never described
   as data.
"""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from numpy.typing import NDArray
from pyproj import Transformer
from rasterio import features as rio_features
from rasterio.enums import Resampling
from rasterio.warp import reproject

from serac.domain.geo import GridSpec
from serac.models.runout.corridor import CorridorFrame
from serac.pipelines.grid import to_affine

F32 = NDArray[np.float32]
F64 = NDArray[np.float64]
BOOL = NDArray[np.bool_]

CORRIDOR_FILENAME = "corridor.geojson"
FILL_EPSILON_M = 1e-3
"""Gradient imposed while filling, small enough not to perturb 30 m terrain measurably."""

THALWEG_THRESHOLD_MU = 0.08
"""The Voellmy Coulomb coefficient the thalweg-slope statistic is stated against.

Every write-up that says "most of this corridor cannot sustain motion" is quoting
`thalweg_fraction_below_mu_threshold` from `reports/runout/terrain.json` at this value, and the
matching slope angle is `atan(0.08) = 4.574 degrees`. It lives here so the claim, the threshold
and the measurement cannot drift apart.
"""

EROSION_MAX_DEPTH_M = 5.0
EROSION_SLOPE_CUTOFF_DEG = 35.0
EROSION_OFFSET_SCALE_M = 150.0

ERODIBLE_ASSUMPTION = (
    f"erodible_depth is a parametric mantle, not a measurement: {EROSION_MAX_DEPTH_M:g} m "
    f"maximum, tapered linearly to zero above {EROSION_SLOPE_CUTOFF_DEG:g} deg slope and by a "
    f"Gaussian of {EROSION_OFFSET_SCALE_M:g} m in cross-channel offset. No sediment-thickness "
    "survey exists for this corridor; the layer encodes an assumption about where alluvium sits."
)

CONDITIONING_ASSUMPTION = (
    "The DEM is priority-flood depression-filled with a 1 mm gradient, seeded from the "
    "downstream outflow edge only, so every corridor cell drains to the outlet. Filling removes "
    "real closed basins as well as artefacts; at 30 m the two are not separable."
)


def read_corridor_polygons(aoi_dir: Path) -> list[dict[str, Any]]:
    """The corridor GeoJSON geometries in WGS 84, as GeoJSON geometry mappings."""
    doc = json.loads((aoi_dir / CORRIDOR_FILENAME).read_text(encoding="utf-8"))
    features = doc["features"] if doc.get("type") == "FeatureCollection" else [doc]
    return [f.get("geometry", f) for f in features]


def _reproject_geometry(geom: dict[str, Any], transformer: Transformer) -> dict[str, Any]:
    def walk(coords: Any) -> Any:
        if isinstance(coords[0], int | float):
            x, y = transformer.transform(coords[0], coords[1])
            return [float(x), float(y)]
        return [walk(c) for c in coords]

    return {"type": geom["type"], "coordinates": walk(geom["coordinates"])}


def coarsen_grid(grid: GridSpec, factor: int) -> GridSpec:
    """A grid `factor` times coarser that contains `grid`.

    `GridSpec` requires the outer edges to be snapped to the resolution, so the south-west
    corner moves outward to the nearest multiple of the coarse resolution rather than being
    inherited; at `factor == 1` the grid is returned unchanged.
    """
    if factor < 1:
        raise ValueError("factor must be >= 1")
    if factor == 1:
        return grid
    res = grid.resolution_m * factor
    x_min = math.floor(grid.x_min / res) * res
    y_min = math.floor(grid.y_min / res) * res
    width = max(1, math.ceil((grid.x_max - x_min) / res))
    height = max(1, math.ceil((grid.y_max - y_min) / res))
    return GridSpec(
        aoi_id=grid.aoi_id,
        epsg=grid.epsg,
        resolution_m=res,
        x_min=x_min,
        y_min=y_min,
        x_max=x_min + width * res,
        y_max=y_min + height * res,
        width=width,
        height=height,
    )


def crop_grid_to_mask(grid: GridSpec, mask: BOOL, margin_cells: int = 3) -> GridSpec:
    """The sub-grid tightly bounding `mask`, grown by `margin_cells` and clipped to `grid`."""
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        raise ValueError("mask is empty")
    r0 = max(0, int(rows[0]) - margin_cells)
    r1 = min(grid.height, int(rows[-1]) + 1 + margin_cells)
    c0 = max(0, int(cols[0]) - margin_cells)
    c1 = min(grid.width, int(cols[-1]) + 1 + margin_cells)
    res = grid.resolution_m
    x_min = grid.x_min + c0 * res
    y_max = grid.y_max - r0 * res
    width = c1 - c0
    height = r1 - r0
    return GridSpec(
        aoi_id=grid.aoi_id,
        epsg=grid.epsg,
        resolution_m=res,
        x_min=x_min,
        y_min=y_max - height * res,
        x_max=x_min + width * res,
        y_max=y_max,
        width=width,
        height=height,
    )


def rasterise_mask(grid: GridSpec, geoms_4326: list[dict[str, Any]]) -> BOOL:
    """Burn WGS 84 polygons onto `grid` (all-touched)."""
    transformer = Transformer.from_crs(4326, grid.epsg, always_xy=True)
    projected = [_reproject_geometry(g, transformer) for g in geoms_4326]
    burned = rio_features.rasterize(
        [(g, 1) for g in projected],
        out_shape=(grid.height, grid.width),
        transform=to_affine(grid),
        fill=0,
        all_touched=True,
        dtype="uint8",
    )
    return np.asarray(burned, dtype=np.uint8).astype(bool)


def reproject_dem(path: Path, grid: GridSpec) -> F32:
    """Bilinear reprojection of a DEM COG onto `grid`; NaN where the source has no data."""
    out = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    with rasterio.open(path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=to_affine(grid),
            dst_crs=f"EPSG:{grid.epsg}",
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return out


def priority_flood_fill(
    elevation: F64, mask: BOOL, seeds: BOOL, epsilon: float
) -> tuple[F64, BOOL]:
    """Priority-flood depression fill restricted to `mask`, seeded at `seeds`.

    Barnes, Lehman & Mulla (2014), "Priority-flood: an optimal depression-filling and
    watershed-labeling algorithm". Every masked cell ends with a path to a seed along which
    elevation strictly decreases by at least `epsilon` per step, so the conditioned surface has
    no closed depressions and drains to the seeded boundary.

    Returns `(filled, reached)`. `reached` is the set of masked cells the flood actually
    visited, i.e. those 8-connected to a seed. Cells outside it are *not* conditioned and are
    dropped from the domain: at 90 m the rasterised corridor pinches into fragments that no
    longer touch the outlet, and leaving them in produced a thalweg that rose 55 m downstream.
    """
    if elevation.shape != mask.shape or mask.shape != seeds.shape:
        raise ValueError("elevation, mask and seeds must have the same shape")
    height, width = elevation.shape
    filled = elevation.copy()
    closed = np.zeros((height, width), dtype=bool)
    heap: list[tuple[float, int, int]] = []
    seeded = seeds & mask
    if not seeded.any():
        raise ValueError("no seed cell lies inside the mask")
    for r, c in zip(*np.nonzero(seeded), strict=True):
        heapq.heappush(heap, (float(filled[r, c]), int(r), int(c)))
        closed[r, c] = True
    neighbours = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in neighbours:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < height and 0 <= nc < width):
                continue
            if closed[nr, nc] or not mask[nr, nc]:
                continue
            closed[nr, nc] = True
            nz = filled[nr, nc]
            if nz <= z + epsilon:
                nz = z + epsilon
                filled[nr, nc] = nz
            heapq.heappush(heap, (float(nz), nr, nc))
    return filled, closed & mask


def slope_degrees(elevation: F64, resolution_m: float) -> F64:
    """Local slope from central differences (Horn's method is overkill at 30 m for a mantle)."""
    dzdy, dzdx = np.gradient(elevation, resolution_m)
    out: F64 = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    return out


@dataclass(frozen=True)
class CorridorTerrain:
    """Everything the solver needs about the ground, on one grid."""

    grid: GridSpec
    elevation_raw: F32
    elevation: F32
    domain_mask: BOOL
    outflow_mask: BOOL
    erodible_depth: F32
    chainage_m: F32
    offset_m: F32
    frame_valid: BOOL
    fill_cells: int
    fill_volume_m3: float
    unreachable_cells: int
    dem_sha256: str
    dem_path: str

    @property
    def shape(self) -> tuple[int, int]:
        return self.domain_mask.shape

    @property
    def cell_area_m2(self) -> float:
        return float(self.grid.resolution_m**2)

    @property
    def active_cells(self) -> int:
        return int(self.domain_mask.sum())

    def thalweg_profile(self, n_bins: int) -> tuple[F64, F64]:
        """`(chainage_centres, minimum conditioned elevation)` in `n_bins` chainage bins."""
        s = np.asarray(self.chainage_m, dtype=np.float64)[self.domain_mask]
        z = np.asarray(self.elevation, dtype=np.float64)[self.domain_mask]
        edges = np.linspace(0.0, float(s.max()), n_bins + 1)
        idx = np.clip(np.searchsorted(edges, s, side="right") - 1, 0, n_bins - 1)
        profile = np.full(n_bins, np.nan)
        for b in range(n_bins):
            sel = z[idx == b]
            if sel.size:
                profile[b] = sel.min()
        centres = 0.5 * (edges[:-1] + edges[1:])
        return centres, profile

    def summary(self) -> dict[str, Any]:
        return {
            "resolution_m": self.grid.resolution_m,
            "shape": [int(self.grid.height), int(self.grid.width)],
            "cells_total": int(self.grid.height * self.grid.width),
            "cells_active": self.active_cells,
            "active_fraction": round(self.active_cells / (self.grid.height * self.grid.width), 4),
            "outflow_cells": int(self.outflow_mask.sum()),
            "elevation_min_m": float(np.nanmin(self.elevation[self.domain_mask])),
            "elevation_max_m": float(np.nanmax(self.elevation[self.domain_mask])),
            "fill_cells": self.fill_cells,
            "unreachable_cells_dropped": self.unreachable_cells,
            "frame_invertible_cells": int(self.frame_valid.sum()),
            "frame_invertible_fraction": round(
                float(self.frame_valid.sum()) / max(self.active_cells, 1), 4
            ),
            "fill_volume_m3": self.fill_volume_m3,
            "erodible_volume_m3": float(
                np.asarray(self.erodible_depth)[self.domain_mask].sum() * self.cell_area_m2
            ),
            "dem_sha256": self.dem_sha256,
            "dem_path": self.dem_path,
        }


def build_terrain(
    *,
    dem_path: Path,
    dem_sha256: str,
    grid: GridSpec,
    corridor_geoms_4326: list[dict[str, Any]],
    frame: CorridorFrame,
    outflow_chainage_m: float | None = None,
) -> CorridorTerrain:
    """Reproject, mask, condition and derive every layer on `grid`."""
    mask = rasterise_mask(grid, corridor_geoms_4326)
    elevation_raw = reproject_dem(dem_path, grid)
    valid = np.isfinite(elevation_raw)
    mask = mask & valid
    if not mask.any():
        raise ValueError("domain mask is empty after intersecting with valid DEM cells")

    res = grid.resolution_m
    cols = grid.x_min + res * (np.arange(grid.width, dtype=np.float64) + 0.5)
    rows = grid.y_max - res * (np.arange(grid.height, dtype=np.float64) + 0.5)
    xx, yy = np.meshgrid(cols, rows)
    s_grid, n_grid = frame.inverse(xx, yy)

    cutoff = outflow_chainage_m if outflow_chainage_m is not None else frame.length_m - 2.0 * res
    outflow = mask & (s_grid >= cutoff)
    if not outflow.any():
        outflow = mask & (s_grid >= np.quantile(s_grid[mask], 0.999))

    raw64 = np.asarray(elevation_raw, dtype=np.float64)
    filled, reached = priority_flood_fill(raw64, mask, outflow, FILL_EPSILON_M)
    dropped = int((mask & ~reached).sum())
    mask = reached
    outflow = outflow & mask
    delta = np.where(mask, filled - raw64, 0.0)
    fill_cells = int(np.count_nonzero(delta > 10.0 * FILL_EPSILON_M))
    fill_volume = float(delta.sum() * res * res)

    slope = slope_degrees(filled, res)
    f_slope = np.clip(1.0 - slope / EROSION_SLOPE_CUTOFF_DEG, 0.0, 1.0)
    f_offset = np.exp(-((n_grid / EROSION_OFFSET_SCALE_M) ** 2))
    erodible = np.where(mask, EROSION_MAX_DEPTH_M * f_slope * f_offset, 0.0)

    return CorridorTerrain(
        grid=grid,
        elevation_raw=elevation_raw,
        elevation=np.asarray(filled, dtype=np.float32),
        domain_mask=mask,
        outflow_mask=outflow,
        erodible_depth=np.asarray(erodible, dtype=np.float32),
        chainage_m=np.asarray(s_grid, dtype=np.float32),
        offset_m=np.asarray(n_grid, dtype=np.float32),
        frame_valid=frame.projection_interior(s_grid) & mask,
        fill_cells=fill_cells,
        fill_volume_m3=fill_volume,
        unreachable_cells=dropped,
        dem_sha256=dem_sha256,
        dem_path=dem_path.as_posix(),
    )


def thalweg_is_draining(terrain: CorridorTerrain, n_bins: int = 400) -> tuple[bool, float]:
    """`(ok, worst_rise_m)`: is the binned thalweg non-increasing downstream after filling?"""
    _, profile = terrain.thalweg_profile(n_bins)
    finite = profile[np.isfinite(profile)]
    if finite.size < 2:
        return False, math.inf
    rises = np.diff(finite)
    worst = float(rises.max())
    return worst <= 1e-6, worst


DEFAULT_AOI = "lhende-khola-trishuli"
DEM_PRODUCT_ID = "glo30_crop_lhende-khola-trishuli"


def find_corridor_dem(repo: Path, aoi_id: str = DEFAULT_AOI) -> tuple[Path, str]:
    """The largest `fetched` GLO-30 crop for `aoi_id` in the ledger, as `(path, sha256)`.

    Largest, because the committed fixture crop covers only the source zone; the corridor crop
    fetched by `serac ingest dem` with the AOI bbox is an order of magnitude bigger. Raising
    here rather than falling back to the fixture is deliberate: a silently-source-zone-only
    corridor would produce runs that stop after 3 km.
    """
    from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
    from serac.domain.manifest import DataSource, ManifestStatus

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    best: tuple[int, Path, str] | None = None
    for entry in ledger.entries():
        if entry.source != DataSource.dem_glo30 or entry.status != ManifestStatus.fetched:
            continue
        if entry.aoi_id != aoi_id or entry.path is None or entry.sha256 is None:
            continue
        path = repo / entry.path
        if not path.exists():
            continue
        size = entry.size_bytes or 0
        if best is None or size > best[0]:
            best = (size, path, entry.sha256)
    if best is None:
        raise FileNotFoundError(
            f"no fetched GLO-30 crop for {aoi_id} in data/manifest.jsonl; run "
            f"`serac runout terrain --fetch` (or `serac ingest dem --aoi {aoi_id} --bbox ...`)"
        )
    return best[1], best[2]


def corridor_terrain(
    repo: Path,
    *,
    aoi_id: str = DEFAULT_AOI,
    resolution_m: float = 30.0,
    half_width_m: float = 1500.0,
) -> CorridorTerrain:
    """Assemble the conditioned corridor terrain at `resolution_m` from committed AOI geometry."""
    from serac.models.runout.corridor import load_frame
    from serac.pipelines.grid import grid_path, load_grid

    aoi_dir = repo / "data" / "aoi" / aoi_id
    base = load_grid(grid_path(repo / "data", aoi_id))
    if resolution_m < base.resolution_m:
        raise ValueError(f"resolution_m={resolution_m} is finer than the AOI grid")
    factor = round(resolution_m / base.resolution_m)
    if not math.isclose(factor * base.resolution_m, resolution_m, rel_tol=1e-9):
        raise ValueError(f"resolution_m={resolution_m} is not a multiple of {base.resolution_m}")
    grid = coarsen_grid(base, factor)
    geoms = read_corridor_polygons(aoi_dir)
    mask = rasterise_mask(grid, geoms)
    grid = crop_grid_to_mask(grid, mask)
    frame = load_frame(aoi_dir, grid.epsg, half_width_m=half_width_m)
    dem_path, dem_sha = find_corridor_dem(repo, aoi_id)
    return build_terrain(
        dem_path=dem_path,
        dem_sha256=dem_sha,
        grid=grid,
        corridor_geoms_4326=geoms,
        frame=frame,
    )


TERRAIN_REPORT_FILENAME = "terrain.json"


def thalweg_statistics(reports_dir: Path, resolution_m: float = 30.0) -> dict[str, Any]:
    """The committed thalweg statistics at `resolution_m`, from `reports/runout/terrain.json`.

    Raises rather than defaulting: a write-up that cannot find the measurement must fail loudly
    instead of quoting a number nobody measured. An earlier draft hardcoded "92% below 6.8
    degrees" -- a real figure, but at the mu=0.12 threshold, attached to a claim about mu=0.08,
    and never committed anywhere. The committed value is 87.4% below 4.574 degrees.
    """
    path = reports_dir / TERRAIN_REPORT_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run scripts/runout_terrain_report.py before quoting "
            "thalweg statistics"
        )
    doc = json.loads(path.read_text(encoding="utf-8"))
    for row in doc["resolutions"]:
        if abs(float(row["resolution_m"]) - resolution_m) < 1e-9:
            return {
                "resolution_m": float(row["resolution_m"]),
                "threshold_mu": float(doc["thalweg_threshold_mu"]),
                "threshold_deg": float(doc["thalweg_threshold_deg"]),
                "fraction_below_threshold": float(row["thalweg_fraction_below_mu_threshold"]),
                "median_slope_deg": float(row["thalweg_slope_deg_p50"]),
                "fraction_below_1_deg": float(row["thalweg_fraction_below_1_deg"]),
                "segments": int(row["thalweg_segments"]),
            }
    raise KeyError(f"{path} has no entry for resolution {resolution_m} m")


def thalweg_sentence(reports_dir: Path, resolution_m: float = 30.0) -> str:
    """The one sentence every M4 write-up leans on, rendered from the measurement."""
    s = thalweg_statistics(reports_dir, resolution_m)
    return (
        f"{s['fraction_below_threshold']:.1%} of this corridor's thalweg is below "
        f"{s['threshold_deg']:.2f} degrees (median {s['median_slope_deg']:.2f} degrees, "
        f"{s['segments']} binned segments at {s['resolution_m']:.0f} m), so a Voellmy Coulomb "
        f"coefficient above {s['threshold_mu']:g} cannot sustain motion over most of it"
    )
