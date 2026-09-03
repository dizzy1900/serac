"""Getting Sentinel-2 scenes and the stable-ground mask onto one grid for feature tracking.

Split out of `optical.py` so the tracker itself takes arrays and can be tested with fictional
images and no filesystem at all.

Scenes come from `EarthSearchSentinel2Adapter`, reusing whatever crops are already committed
under `data/fixtures/sentinel2_earthsearch/` before fetching anything new, and every fetched
byte goes through that adapter's ledger as usual. Feature tracking uses **B03** (green, 10 m):
the finest band the adapter fetches, and the one whose contrast over rock and snow is best.

The tracking grid is the AOI grid resampled to the B03 pixel, so chip coordinates map onto
slope-unit labels without a second reprojection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

from serac.errors import SeracError
from serac.models.watch.optical import STABLE_MAX_SLOPE_DEG

TRACKING_BAND: Final[str] = "B03"
TRACKING_PIXEL_M: Final[float] = 10.0
MAX_CLOUD_PERCENT: Final[float] = 20.0
SCL_CLOUD_CLASSES: Final[frozenset[int]] = frozenset({3, 8, 9, 10})
SCL_WATER_CLASS: Final[int] = 6
MAX_AOI_CLOUD_FRACTION: Final[float] = 0.35

SEASON_WINDOW: Final[tuple[tuple[int, int], tuple[int, int]]] = ((9, 15), (12, 15))
"""Post-monsoon (15 September - 15 December), and one scene per year inside it.

Season-matching the pairs is the whole point. High Mountain Asia is under cloud through the
monsoon and under snow through the winter, and a summer/winter pair correlates on the snow
line rather than on the terrain. Taking the least-cloudy scene from the same post-monsoon
window each year makes every pair roughly annual and roughly snow-matched, which is the only
configuration in which a slow slope displacement is separable from a seasonal surface change.
The cost is a one-year sampling interval: this layer cannot see anything faster.
"""

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class Scene:
    """One usable Sentinel-2 acquisition on the tracking grid."""

    product_id: str
    acquired_at: datetime
    band: FloatArray
    cloud_fraction: float
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "acquired_at": self.acquired_at.isoformat(),
            "cloud_fraction": round(self.cloud_fraction, 4),
            "path": self.path,
        }


@dataclass(frozen=True)
class SceneStack:
    """Every usable scene plus the slope-unit labels on the same grid."""

    scenes: list[Scene]
    labels: NDArray[np.int32]
    unit_ids: dict[int, str]
    pixel_m: float
    transform: Affine
    epsg: int


def tracking_grid(grid: Any, pixel_m: float = TRACKING_PIXEL_M) -> tuple[Affine, int, int]:
    """The AOI grid at the B03 pixel: same origin, finer sampling."""
    factor = grid.resolution_m / pixel_m
    width = round(grid.width * factor)
    height = round(grid.height * factor)
    transform = Affine(pixel_m, 0.0, grid.x_min, 0.0, -pixel_m, grid.y_max)
    return transform, width, height


def _warp(
    source: NDArray[Any],
    src_transform: Affine,
    src_crs: Any,
    dst_transform: Affine,
    dst_crs: Any,
    shape: tuple[int, int],
    *,
    resampling: Resampling,
    dtype: str = "float64",
    nodata: float = float("nan"),
) -> NDArray[Any]:
    destination = np.full(shape, nodata, dtype=dtype)
    reproject(
        source=source,
        destination=destination,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        src_nodata=nodata,
        dst_nodata=nodata,
        resampling=resampling,
    )
    return destination


def load_scene_stack(
    *,
    data_dir: Path,
    aoi_dir: Path,
    aoi_id: str,
    window_start: datetime,
    window_end: datetime,
    online: bool = False,
) -> SceneStack:
    """Assemble the tracking stack from committed crops, fetching more only when `online`."""
    import geopandas as gpd

    from serac.models.watch.raster import load_grid_spec
    from serac.models.watch.slope_units import labels_path, slope_units_path

    grid = load_grid_spec(aoi_dir)
    transform, width, height = tracking_grid(grid)
    dst_crs = CRS.from_epsg(grid.epsg)

    if online:
        _fetch_scenes(
            data_dir=data_dir,
            aoi_dir=aoi_dir,
            aoi_id=aoi_id,
            window_start=window_start,
            window_end=window_end,
        )

    scenes: list[Scene] = []
    for band_path in sorted(_candidate_band_files(data_dir, aoi_id)):
        acquired = _acquisition_time(band_path)
        if acquired is None or not (window_start <= acquired <= window_end):
            continue
        with rasterio.open(band_path) as src:
            band = _warp(
                src.read(1).astype(np.float64),
                src.transform,
                src.crs,
                transform,
                dst_crs,
                (height, width),
                resampling=Resampling.bilinear,
            )
        cloud = _cloud_fraction(band_path, transform, dst_crs, (height, width))
        if cloud > MAX_AOI_CLOUD_FRACTION:
            continue
        if not np.isfinite(band).any():
            continue
        scenes.append(
            Scene(
                product_id=band_path.parent.name,
                acquired_at=acquired,
                band=np.nan_to_num(band, nan=0.0),
                cloud_fraction=cloud,
                path=band_path.as_posix(),
            )
        )
    scenes.sort(key=lambda s: (s.acquired_at, s.product_id))

    labels_file = labels_path(data_dir, aoi_id)
    if not labels_file.exists():
        raise SeracError(f"no slope-unit labels at {labels_file}; run `serac watch slope-units`")
    with rasterio.open(labels_file) as src:
        labels = _warp(
            src.read(1).astype(np.int32),
            src.transform,
            src.crs,
            transform,
            dst_crs,
            (height, width),
            resampling=Resampling.nearest,
            dtype="int32",
            nodata=0.0,
        ).astype(np.int32)
    frame = gpd.read_parquet(slope_units_path(data_dir, aoi_id))
    unit_ids = {
        int(r.unit_index): str(r.unit_id)
        for r in frame.itertuples()
        if (labels == int(r.unit_index)).any()
    }
    return SceneStack(
        scenes=scenes,
        labels=labels,
        unit_ids=unit_ids,
        pixel_m=TRACKING_PIXEL_M,
        transform=transform,
        epsg=grid.epsg,
    )


def stable_ground_mask(
    stack: SceneStack, *, data_dir: Path, aoi_dir: Path, aoi_id: str
) -> NDArray[np.bool_]:
    """Slope below 10 degrees, outside every RGI 7.0 outline, and not water.

    Water is excluded by geometry rather than by an SCL class here: the SCL water class is
    unreliable over shadowed mountain rivers, and a river's specular return would inflate the
    floor. Slope-unit terrain steeper than the threshold is already excluded, so what remains
    is valley floor and gentle plateau, which is what a noise floor should be measured on.
    """
    import rasterio.features

    from serac.models.watch.geometry import slope_aspect
    from serac.models.watch.glaciers import fetch_rgi7
    from serac.models.watch.raster import aoi_dem, grid_transform

    dem = aoi_dem(data_dir, aoi_dir, aoi_id)
    elevation = np.where(
        np.isfinite(dem.elevation_m), dem.elevation_m, float(np.nanmedian(dem.elevation_m))
    )
    slope, _aspect = slope_aspect(elevation, dem.grid.resolution_m, dem.grid.resolution_m)
    slope_fine = _warp(
        slope,
        grid_transform(dem.grid),
        CRS.from_epsg(dem.grid.epsg),
        stack.transform,
        CRS.from_epsg(stack.epsg),
        stack.labels.shape,
        resampling=Resampling.bilinear,
    )
    mask = np.isfinite(slope_fine) & (slope_fine < STABLE_MAX_SLOPE_DEG)

    glaciers = fetch_rgi7(data_dir=data_dir, aoi_dir=aoi_dir, aoi_id=aoi_id, online=False).to_crs(
        stack.epsg
    )
    if glaciers.available and glaciers.geometries is not None:
        frame: Any = glaciers.geometries
        if len(frame) > 0:
            burned = rasterio.features.rasterize(
                ((geom, 1) for geom in frame.geometry),
                out_shape=stack.labels.shape,
                transform=stack.transform,
                fill=0,
                dtype="uint8",
            )
            mask &= burned == 0
    return np.asarray(mask, dtype=np.bool_)


def _candidate_band_files(data_dir: Path, aoi_id: str) -> list[Path]:
    """Committed fixture crops first, then anything fetched into `data/raw`."""
    roots = [
        data_dir / "fixtures" / "sentinel2_earthsearch" / aoi_id,
        data_dir / "raw" / "sentinel2_earthsearch" / aoi_id,
    ]
    out: list[Path] = []
    for root in roots:
        if root.exists():
            out.extend(root.glob(f"*/*{TRACKING_BAND}*.tif"))
    return out


def _acquisition_time(band_path: Path) -> datetime | None:
    """Acquisition time from the scene's committed STAC item, or from its product id."""
    item_path = band_path.parent / "item.json"
    if item_path.exists():
        item = json.loads(item_path.read_text(encoding="utf-8"))
        stamp = (item.get("properties") or {}).get("datetime")
        if stamp:
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    name = band_path.parent.name
    for token in name.replace("-", "_").split("_"):
        if len(token) >= 8 and token[:8].isdigit():
            try:
                return datetime.strptime(token[:8], "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:  # pragma: no cover - defensive
                continue
    return None


def _cloud_fraction(
    band_path: Path, transform: Affine, dst_crs: Any, shape: tuple[int, int]
) -> float:
    """Cloud fraction from the scene's SCL crop; 0.0 when no SCL was fetched."""
    scl = next(iter(band_path.parent.glob("*SCL*.tif")), None)
    if scl is None:
        return 0.0
    with rasterio.open(scl) as src:
        classes = _warp(
            src.read(1).astype(np.float64),
            src.transform,
            src.crs,
            transform,
            dst_crs,
            shape,
            resampling=Resampling.nearest,
        )
    valid = np.isfinite(classes)
    if not valid.any():
        return 1.0
    cloudy = np.isin(classes[valid].astype(np.int32), list(SCL_CLOUD_CLASSES))
    return float(cloudy.mean())


def in_season(
    when: datetime, season: tuple[tuple[int, int], tuple[int, int]] = SEASON_WINDOW
) -> bool:
    """Is `when` inside the post-monsoon window? Handles a window that wraps the new year."""
    (m0, d0), (m1, d1) = season
    start, end = (m0, d0), (m1, d1)
    here = (when.month, when.day)
    if start <= end:
        return start <= here <= end
    return here >= start or here <= end


def _fetch_scenes(
    *,
    data_dir: Path,
    aoi_dir: Path,
    aoi_id: str,
    window_start: datetime,
    window_end: datetime,
    per_year: int = 1,
) -> None:
    """Fetch Sentinel-2 windows through the existing adapter, ledgering every byte."""
    from serac.adapters.eo.earthsearch_sentinel2 import (
        EarthSearchSentinel2Adapter,
        PystacSearchClient,
    )
    from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
    from serac.ports.ingest import IngestRequest

    bbox_raw = json.loads((aoi_dir / "aoi.json").read_text(encoding="utf-8"))[
        "cube_extent_bbox_4326"
    ]
    adapter = EarthSearchSentinel2Adapter(PystacSearchClient(), repo_root=data_dir.resolve().parent)
    request = IngestRequest(
        aoi_id=aoi_id,
        bbox_4326=(
            float(bbox_raw[0]),
            float(bbox_raw[1]),
            float(bbox_raw[2]),
            float(bbox_raw[3]),
        ),
        time_start=window_start,
        time_end=window_end,
        params={"max_cloud": MAX_CLOUD_PERCENT},
    )
    found = adapter.search(request)
    in_window = [p for p in found if p.time_start is not None and in_season(p.time_start)]
    by_year: dict[int, list[Any]] = {}
    for product in in_window:
        assert product.time_start is not None
        by_year.setdefault(product.time_start.year, []).append(product)
    chosen: list[Any] = []
    for year in sorted(by_year):
        ranked = sorted(
            by_year[year],
            key=lambda p: (
                float(p.properties.get("eo:cloud_cover", 100.0) or 100.0),
                p.product_id,
            ),
        )
        chosen.extend(ranked[:per_year])
    if not chosen:
        return
    plan = adapter.plan(request).model_copy(update={"products": chosen})
    adapter.fetch(
        plan,
        dest_root=data_dir,
        ledger=JsonlManifestLedger(data_dir / "manifest.jsonl"),
        confirm=lambda _q: True,
    )
