"""Delineate slope units from the GLO-30 DEM, deterministically and with a hash to prove it.

`SlopeUnit` was a contract with no producer until now. This module is the producer.

The method, stated plainly
--------------------------
This is **not** `r.slopeunits` (Alvioli et al.) and it is not a hydrological half-basin
delineation. GRASS is not available on this machine (its containers are amd64-only) and no
pure-Python half-basin implementation was obtainable, so serac uses its own segmentation and
says so everywhere the units are mentioned:

1. Slope and aspect from a Horn third-order operator on the AOI's 30 m projected grid.
2. Aspect is smoothed by a circular (vector) mean over a 5x5 window, because raw aspect on a
   30 m DEM speckles badly and would shatter a coherent face into dozens of slivers.
3. Terrain with slope below `MIN_SLOPE_DEG` is excluded: valley floors and plateaux are not
   rock-slope units and including them would dominate every cross-unit statistic.
4. Each remaining pixel is labelled by (aspect octant, elevation band), and the units are the
   connected components of that label field under 8-connectivity.
5. Components below `min_area_m2` are dissolved into the neighbouring component with which
   they share the longest boundary; those with no neighbour are dropped.

What this buys and what it costs: the units follow aspect and elevation, so a unit has a
coherent viewing geometry and a coherent seasonal signal, which is what the anomaly model
needs. What it loses is hydrological meaning — these units do not correspond to half-basins
and should not be compared with a published slope-unit inventory as if they did.

Determinism is enforced end to end: the same DEM crop and the same parameters give the same
label raster byte for byte, and `delineation_sha256` covers the labels, the parameters and the
source DEM's own checksum.

`SlopeUnit.glacier_cover` is a non-nullable `bool`. When glacier outlines cannot be fetched,
the parquet index is still written but **no `SlopeUnit` records are emitted at all** — the
contract is not relaxed and the gap is reported.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import rasterio.features
from numpy.typing import NDArray
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

from serac.models.watch.geometry import slope_aspect
from serac.models.watch.raster import GriddedDem, aoi_dem, grid_transform

MIN_SLOPE_DEG: Final[float] = 15.0
"""Below this a pixel is valley floor or plateau, not a rock-slope unit."""

ASPECT_OCTANTS: Final[int] = 8
ELEVATION_BAND_M: Final[float] = 250.0
ASPECT_SMOOTH_WINDOW: Final[int] = 5
DEFAULT_MIN_AREA_M2: Final[float] = 40_000.0

METHOD_ID: Final[str] = "serac-aspect-elevation-cc-v1"
METHOD_DESCRIPTION: Final[str] = (
    "Connected components of (aspect octant, 250 m elevation band) over terrain steeper than "
    "15 degrees, on the AOI's 30 m grid, with a 5x5 circular-mean aspect smoother and small "
    "components dissolved into their longest-shared-boundary neighbour. NOT r.slopeunits and "
    "not a hydrological half-basin delineation."
)

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int32]


@dataclass(frozen=True)
class Delineation:
    """The label raster and everything needed to reproduce or audit it."""

    labels: IntArray
    slope_deg: FloatArray
    aspect_deg: FloatArray
    dem: GriddedDem
    params: dict[str, Any]

    @property
    def unit_ids(self) -> list[int]:
        return [int(v) for v in np.unique(self.labels) if v > 0]

    def digest(self) -> str:
        """sha256 over the labels, the parameters and the source DEM's checksum."""
        h = hashlib.sha256()
        h.update(json.dumps(self.params, sort_keys=True).encode("utf-8"))
        h.update(self.dem.source_sha256.encode("utf-8"))
        h.update(np.ascontiguousarray(self.labels, dtype=np.int32).tobytes())
        return h.hexdigest()


def _circular_mean_aspect(aspect_deg: FloatArray, window: int) -> FloatArray:
    """Vector (circular) mean of aspect over a square window; the arithmetic mean is wrong here.

    Averaging 350 and 10 degrees arithmetically gives 180 — the exact opposite direction — so
    the smoothing has to go through the unit vectors.
    """
    radians = np.radians(aspect_deg)
    sin_mean = ndimage.uniform_filter(np.sin(radians), size=window, mode="nearest")
    cos_mean = ndimage.uniform_filter(np.cos(radians), size=window, mode="nearest")
    smoothed: FloatArray = np.mod(np.degrees(np.arctan2(sin_mean, cos_mean)), 360.0)
    return smoothed


STRUCTURE_8 = np.ones((3, 3), dtype=bool)


def _dissolve_small(labels: IntArray, min_pixels: int) -> IntArray:
    """Merge components below `min_pixels` into the neighbour they share the most edge with.

    Iterated until nothing changes, smallest component first, so the result does not depend on
    label numbering. A component with no labelled neighbour is dropped (set to 0).

    Every component is handled inside its own padded bounding box rather than by scanning the
    whole raster. The rule is identical either way — a component's neighbours all touch it —
    but the cost stops being (number of small components) x (raster size), which on the
    2106 x 2285 Langtang grid is the difference between minutes and never finishing.
    """
    out = labels.copy()
    height, width = out.shape
    while True:
        ids, counts = np.unique(out[out > 0], return_counts=True)
        sizes = {int(i): int(c) for i, c in zip(ids, counts, strict=True)}
        small = sorted((i for i, c in sizes.items() if c < min_pixels), key=lambda i: (sizes[i], i))
        if not small:
            return out
        boxes = ndimage.find_objects(out)
        changed = False
        for unit in small:
            box = boxes[unit - 1] if unit - 1 < len(boxes) else None
            if box is None:
                continue
            rows = slice(max(box[0].start - 1, 0), min(box[0].stop + 1, height))
            cols = slice(max(box[1].start - 1, 0), min(box[1].stop + 1, width))
            window = out[rows, cols]
            mask = window == unit
            if not mask.any():
                continue
            border = ndimage.binary_dilation(mask, structure=STRUCTURE_8) & ~mask
            neighbours = window[border]
            neighbours = neighbours[neighbours > 0]
            if neighbours.size == 0:
                window[mask] = 0
                changed = True
                continue
            values, counts_n = np.unique(neighbours, return_counts=True)
            best = int(values[np.lexsort((values, -counts_n))[0]])
            window[mask] = best
            changed = True
        if not changed:
            return out


def delineate(
    dem: GriddedDem,
    *,
    min_slope_deg: float = MIN_SLOPE_DEG,
    elevation_band_m: float = ELEVATION_BAND_M,
    min_area_m2: float = DEFAULT_MIN_AREA_M2,
    aspect_smooth_window: int = ASPECT_SMOOTH_WINDOW,
) -> Delineation:
    """Run the segmentation described in the module docstring."""
    elevation = dem.elevation_m
    finite = np.isfinite(elevation)
    if not finite.any():
        raise ValueError("the DEM crop has no finite elevations")
    filled = np.where(finite, elevation, float(np.nanmedian(elevation)))
    slope, aspect_raw = slope_aspect(filled, dem.grid.resolution_m, dem.grid.resolution_m)
    aspect = _circular_mean_aspect(aspect_raw, aspect_smooth_window)

    mask = finite & (slope >= min_slope_deg)
    octant = np.floor_divide(
        np.mod(aspect + 360.0 / (2 * ASPECT_OCTANTS), 360.0), 360.0 / ASPECT_OCTANTS
    )
    band = np.floor_divide(filled, elevation_band_m)
    band = band - band[mask].min() if mask.any() else band
    combined = (band.astype(np.int64) * ASPECT_OCTANTS + octant.astype(np.int64) + 1) * mask

    structure = np.ones((3, 3), dtype=bool)
    labels = np.zeros(combined.shape, dtype=np.int32)
    next_label = 1
    for value in np.unique(combined[combined > 0]):
        component, n = ndimage.label(combined == value, structure=structure)
        if n == 0:
            continue
        labels = np.where(component > 0, component.astype(np.int32) + next_label - 1, labels)
        next_label += n

    pixel_area = dem.grid.resolution_m**2
    labels = _dissolve_small(labels, max(round(min_area_m2 / pixel_area), 1))
    labels = _renumber(labels)
    params = {
        "method_id": METHOD_ID,
        "min_slope_deg": min_slope_deg,
        "elevation_band_m": elevation_band_m,
        "min_area_m2": min_area_m2,
        "aspect_smooth_window": aspect_smooth_window,
        "aspect_octants": ASPECT_OCTANTS,
        "resolution_m": dem.grid.resolution_m,
        "epsg": dem.grid.epsg,
        "dem_resampling": dem.resampling,
    }
    return Delineation(labels=labels, slope_deg=slope, aspect_deg=aspect, dem=dem, params=params)


def _renumber(labels: IntArray) -> IntArray:
    """Contiguous 1..n labels in a deterministic order (first appearance, row-major)."""
    out = np.zeros_like(labels)
    seen: dict[int, int] = {}
    for value in labels.ravel():
        v = int(value)
        if v > 0 and v not in seen:
            seen[v] = len(seen) + 1
    for old, new in seen.items():
        out[labels == old] = new
    return out


def _circular_mean_deg(values: FloatArray) -> float:
    radians = np.radians(values)
    return float(
        np.mod(np.degrees(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())), 360.0)
    )


def unit_table(delin: Delineation) -> list[dict[str, Any]]:
    """One row per slope unit: geometry, aspect, slope, elevation band, area, pixel count."""
    transform = grid_transform(delin.dem.grid)
    rows: list[dict[str, Any]] = []
    pixel_area = delin.dem.grid.resolution_m**2
    geometries: dict[int, list[BaseGeometry]] = {}
    for geom, value in rasterio.features.shapes(
        delin.labels, mask=delin.labels > 0, transform=transform, connectivity=8
    ):
        geometries.setdefault(int(value), []).append(shape(geom))
    for unit_id in delin.unit_ids:
        mask = delin.labels == unit_id
        elevations = delin.dem.elevation_m[mask]
        parts = geometries.get(unit_id, [])
        geometry = parts[0] if len(parts) == 1 else _union(parts)
        rows.append(
            {
                "unit_id": f"su-{unit_id:05d}",
                "unit_index": unit_id,
                "n_pixels": int(mask.sum()),
                "area_m2": float(mask.sum() * pixel_area),
                "mean_slope_deg": float(delin.slope_deg[mask].mean()),
                "aspect_deg": _circular_mean_deg(delin.aspect_deg[mask]),
                "elevation_min_m": float(np.nanmin(elevations)),
                "elevation_max_m": float(np.nanmax(elevations)),
                "elevation_mean_m": float(np.nanmean(elevations)),
                "geometry": geometry,
            }
        )
    return rows


def _union(parts: list[BaseGeometry]) -> BaseGeometry:
    from shapely.ops import unary_union

    return unary_union(parts)


def slope_units_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "interim" / "watch" / f"slope_units_{aoi_id}.parquet"


def labels_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "interim" / "watch" / f"slope_unit_labels_{aoi_id}.tif"


def build_slope_units(
    *,
    data_dir: Path,
    reports_dir: Path,
    aoi_dir: Path,
    aoi_id: str,
    min_area_m2: float = DEFAULT_MIN_AREA_M2,
    online: bool = False,
) -> dict[str, Any]:
    """Delineate, write the parquet and label raster, emit `SlopeUnit` records if RGI landed."""
    import geopandas as gpd

    from serac.models.watch.glaciers import GlacierOutlines, fetch_rgi7

    dem = aoi_dem(data_dir, aoi_dir, aoi_id)
    delin = delineate(dem, min_area_m2=min_area_m2)
    rows = unit_table(delin)
    digest = delin.digest()

    glaciers: GlacierOutlines = fetch_rgi7(
        data_dir=data_dir, aoi_dir=aoi_dir, aoi_id=aoi_id, online=online
    ).to_crs(dem.grid.epsg)
    for row in rows:
        row["glacier_cover_fraction"] = glaciers.cover_fraction(row["geometry"])
        row["glacier_cover"] = (
            None if not glaciers.available else bool(row["glacier_cover_fraction"] > 0.0)
        )

    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{dem.grid.epsg}")
    frame = frame.assign(
        aoi_id=aoi_id,
        method_id=METHOD_ID,
        delineation_sha256=digest,
        dem_sha256=dem.source_sha256,
        dem_path=dem.source_path,
    )
    out_path = slope_units_path(data_dir, aoi_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out_path, index=False)
    _write_labels(delin, labels_path(data_dir, aoi_id))

    records_written = 0
    records_path: str | None = None
    if glaciers.available:
        # `SlopeUnit.geometry` is RFC 7946, so it is longitude/latitude and bounds-checked.
        # The parquet keeps the projected geometry because every area here is metric.
        wgs84 = frame.geometry.to_crs("EPSG:4326")
        records = _slope_unit_records(
            rows,
            aoi_id=aoi_id,
            source_refs=glaciers.source_refs,
            geometries_4326=list(wgs84),
        )
        target = data_dir / "interim" / "watch" / f"slope_units_{aoi_id}.jsonl"
        target.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
        )
        records_written = len(records)
        records_path = target.as_posix()

    summary = {
        "aoi_id": aoi_id,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "method_id": METHOD_ID,
        "method_description": METHOD_DESCRIPTION,
        "delineation_sha256": digest,
        "dem_source_path": dem.source_path,
        "dem_source_sha256": dem.source_sha256,
        "parameters": delin.params,
        "n_units": len(rows),
        "total_area_km2": round(sum(r["area_m2"] for r in rows) / 1e6, 3),
        "median_area_km2": round(float(np.median([r["area_m2"] for r in rows])) / 1e6, 4)
        if rows
        else None,
        "parquet_path": out_path.as_posix(),
        "labels_path": labels_path(data_dir, aoi_id).as_posix(),
        "glacier_outlines": glaciers.status(),
        "slope_unit_records_written": records_written,
        "slope_unit_records_path": records_path,
        "known_gap": None
        if glaciers.available
        else (
            "SlopeUnit.glacier_cover is a non-nullable bool and no glacier outlines were "
            "fetched, so no SlopeUnit records were emitted. The parquet index is written and "
            "carries glacier_cover=null. The contract was not relaxed."
        ),
    }
    report = reports_dir / "watch" / f"slope_units_{aoi_id}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def _write_labels(delin: Delineation, path: Path) -> Path:
    import rasterio
    from rasterio.crs import CRS

    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": "int32",
        "count": 1,
        "width": delin.dem.grid.width,
        "height": delin.dem.grid.height,
        "crs": CRS.from_epsg(delin.dem.grid.epsg),
        "transform": grid_transform(delin.dem.grid),
        "nodata": 0,
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(delin.labels, 1)
    return path


def _slope_unit_records(
    rows: list[dict[str, Any]],
    *,
    aoi_id: str,
    source_refs: list[str],
    geometries_4326: list[BaseGeometry],
) -> list[dict[str, Any]]:
    """`SlopeUnit`-shaped dicts, validated against the domain model before being written."""
    from serac.domain.geo import SlopeUnit

    out: list[dict[str, Any]] = []
    for row, geometry_4326 in zip(rows, geometries_4326, strict=True):
        unit = SlopeUnit.model_validate(
            {
                "id": row["unit_id"],
                "aoi_id": aoi_id,
                "geometry": json.loads(json.dumps(mapping(geometry_4326))),
                "aspect_deg": row["aspect_deg"],
                "mean_slope_deg": row["mean_slope_deg"],
                "elevation_band_m": (row["elevation_min_m"], row["elevation_max_m"]),
                "glacier_cover": bool(row["glacier_cover"]),
                "area_m2": row["area_m2"],
                "geometry_quality": "surveyed",
                "source_refs": source_refs,
                "notes": (
                    f"Delineated by {METHOD_ID} from Copernicus GLO-30. {METHOD_DESCRIPTION} "
                    f"glacier_cover_fraction={row['glacier_cover_fraction']:.3f}."
                ),
            }
        )
        out.append(unit.model_dump(mode="json"))
    return out
