"""Copernicus GLO-30 DEM adapter (public COG tiles on AWS, no credentials).

Tiles are 1°x1° GeoTIFFs named `Copernicus_DSM_COG_10_<N|S>lat_00_<E|W>lon_00_DEM` where
`lat`/`lon` are the integer degrees of the tile's south-west corner (`S01` covers -1..0,
`W080` covers -80..-79). Pixels are 1 arc-second, `AREA_OR_POINT=Point`: the top-left pixel
centre of `N30_E079` sits exactly at (79.0 E, 31.0 N), so the tile's edge extent is
`[lon - 1/7200, lon + 1 - 1/7200] x (lat + 1/7200, lat + 1 + 1/7200]`. Longitude spacing
widens above 50° N/S; the adapter reads whatever the tile's own transform says.

Default mode reads a window (bbox + buffer, snapped to the tile grid) straight from the
public COGs over HTTP range requests and writes a single float32 COG in EPSG:4326, exactly
as delivered (no reprojection, no resampling). `--full-tiles` streams whole tiles instead.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import rasterio
from rasterio.merge import merge as rasterio_merge

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import HttpClient, HttpxClient, sha256_and_size
from serac.domain.manifest import DataSource
from serac.ports.dem import AffineCoefficients, Bbox4326, DemProvider, DemWindow
from serac.ports.ingest import DryRunPlan, IngestRequest, ProductRecord

GLO30_BUCKET_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
GLO30_LICENCE = (
    "Copernicus DEM (GLO-30 Public): free for the general public under the Copernicus DEM "
    "licence (ESA / Airbus DS); attribution 'Produced using Copernicus WorldDEM-30 (c) DLR "
    "e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS "
    "by the European Union and ESA; all rights reserved'"
)
GLO30_LICENCE_URL = (
    "https://dataspace.copernicus.eu/explore-data/data-collections/"
    "copernicus-contributing-missions/collections-description/COP-DEM"
)
PIXEL_DEG = 1.0 / 3600.0
HALF_PIXEL_DEG = PIXEL_DEG / 2.0
BYTES_PER_SAMPLE = 4  # float32
DEFAULT_BUFFER_M = 2000.0
CROP_FILENAME = "glo30_crop.tif"
METRES_PER_DEGREE_LAT = 111_320.0

RasterOpener = Callable[[str], AbstractContextManager[Any]]
"""Opens a raster by URL/path as a context manager yielding a rasterio dataset (Any)."""


def tile_name(lat_index: int, lon_index: int) -> str:
    """Tile id for the 1° cell whose south-west corner is (`lat_index`, `lon_index`)."""
    if not -90 <= lat_index <= 89:
        raise ValueError(f"lat_index {lat_index} outside [-90, 89]")
    lon_index = ((lon_index + 180) % 360) - 180
    ns = "N" if lat_index >= 0 else "S"
    ew = "E" if lon_index >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_index):02d}_00_{ew}{abs(lon_index):03d}_00_DEM"


def tile_url(name: str, bucket_url: str = GLO30_BUCKET_URL) -> str:
    return f"{bucket_url}/{name}/{name}.tif"


def tile_indices_for_bbox(bbox: Bbox4326) -> list[tuple[int, int]]:
    """(lat_index, lon_index) of every tile whose pixel extent intersects `bbox`.

    Uses the half-pixel-shifted tile extent described in the module docstring, so a bbox
    edge exactly on an integer degree does not drag in the neighbouring tile.
    """
    w, s, e, n = bbox
    lat_lo = math.floor(s - HALF_PIXEL_DEG)
    lat_hi = math.floor(n - HALF_PIXEL_DEG)
    lon_lo = math.floor(w + HALF_PIXEL_DEG)
    lon_hi = math.floor(e + HALF_PIXEL_DEG)
    lat_lo, lat_hi = max(lat_lo, -90), min(lat_hi, 89)
    return [(lat, lon) for lat in range(lat_lo, lat_hi + 1) for lon in range(lon_lo, lon_hi + 1)]


def tiles_for_bbox(bbox: Bbox4326) -> list[str]:
    """Tile ids intersecting `bbox`, south-to-north then west-to-east."""
    return [tile_name(lat, lon) for lat, lon in tile_indices_for_bbox(bbox)]


def buffered_bbox(bbox: Bbox4326, buffer_m: float) -> Bbox4326:
    """Expand `bbox` by `buffer_m` metres on every side (spherical degrees, clamped)."""
    if buffer_m < 0:
        raise ValueError("buffer_m must be >= 0")
    w, s, e, n = bbox
    dlat = buffer_m / METRES_PER_DEGREE_LAT
    mid_lat = (s + n) / 2.0
    dlon = buffer_m / (METRES_PER_DEGREE_LAT * max(math.cos(math.radians(mid_lat)), 1e-6))
    return (max(w - dlon, -180.0), max(s - dlat, -90.0), min(e + dlon, 180.0), min(n + dlat, 90.0))


def snap_bounds_to_grid(bbox: Bbox4326) -> Bbox4326:
    """Grow `bbox` outward to GLO-30 pixel edges (lon edges at k*px - px/2, lat at j*px + px/2)."""
    w, s, e, n = bbox
    west = math.floor((w + HALF_PIXEL_DEG) / PIXEL_DEG) * PIXEL_DEG - HALF_PIXEL_DEG
    east = math.ceil((e + HALF_PIXEL_DEG) / PIXEL_DEG) * PIXEL_DEG - HALF_PIXEL_DEG
    south = math.floor((s - HALF_PIXEL_DEG) / PIXEL_DEG) * PIXEL_DEG + HALF_PIXEL_DEG
    north = math.ceil((n - HALF_PIXEL_DEG) / PIXEL_DEG) * PIXEL_DEG + HALF_PIXEL_DEG
    return (west, south, east, north)


def window_shape(bounds: Bbox4326) -> tuple[int, int]:
    """(rows, cols) of a grid-snapped window at 1 arc-second spacing."""
    w, s, e, n = bounds
    return round((n - s) / PIXEL_DEG), round((e - w) / PIXEL_DEG)


def _default_opener(url: str) -> AbstractContextManager[Any]:
    return rasterio.open(url)  # type: ignore[no-any-return]


class Glo30DemAdapter(BaseIngestAdapter, DemProvider):
    """Windowed (default) or full-tile ingestion of Copernicus GLO-30; also a `DemProvider`."""

    source: ClassVar[DataSource] = DataSource.dem_glo30
    adapter_name: ClassVar[str] = "dem_glo30"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = GLO30_LICENCE
    licence_source_url: ClassVar[str | None] = GLO30_LICENCE_URL

    provider_name: ClassVar[str] = "Copernicus GLO-30"
    native_resolution_m: ClassVar[float] = 30.0

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        raster_opener: RasterOpener | None = None,
        bucket_url: str = GLO30_BUCKET_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._http = http
        self._open = raster_opener or _default_opener
        self.bucket_url = bucket_url

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpxClient()
        return self._http

    # -- request parameters -------------------------------------------------------------------

    @staticmethod
    def _buffer_m(request: IngestRequest) -> float:
        return float(request.params.get("buffer_m", DEFAULT_BUFFER_M))

    @staticmethod
    def _full_tiles(request: IngestRequest) -> bool:
        return bool(request.params.get("full_tiles", False))

    def crop_bounds(self, request: IngestRequest) -> Bbox4326:
        return snap_bounds_to_grid(buffered_bbox(request.bbox_4326, self._buffer_m(request)))

    # -- port: search / plan --------------------------------------------------------------------

    def _tile_record(self, name: str, lat: int, lon: int, size: int | None) -> ProductRecord:
        return ProductRecord(
            source=self.source,
            product_id=name,
            product_level="GLO-30",
            url=tile_url(name, self.bucket_url),
            bbox_4326=(
                lon - HALF_PIXEL_DEG,
                lat + HALF_PIXEL_DEG,
                lon + 1 - HALF_PIXEL_DEG,
                lat + 1 + HALF_PIXEL_DEG,
            ),
            estimated_bytes=size,
            licence=self.licence,
            licence_source_url=self.licence_source_url,
            properties={"tile_lat": lat, "tile_lon": lon},
        )

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """The tiles intersecting the buffered bbox (no network)."""
        bounds = self.crop_bounds(request)
        return [
            self._tile_record(tile_name(lat, lon), lat, lon, None)
            for lat, lon in tile_indices_for_bbox(bounds)
        ]

    def plan(self, request: IngestRequest) -> DryRunPlan:
        tiles = self.search(request)
        bounds = self.crop_bounds(request)
        warnings: list[str] = []
        if bounds[3] > 50.0 or bounds[1] < -50.0:
            warnings.append(
                "above 50 deg latitude GLO-30 longitude spacing exceeds 1 arc-second; "
                "the estimate assumes 1 arc-second"
            )
        if self._full_tiles(request):
            sizes: list[int | None] = []
            for t in tiles:
                assert t.url is not None
                try:
                    sizes.append(self.http.head_content_length(t.url))
                except Exception as exc:  # network unreachable or 404: say so, do not guess
                    warnings.append(f"HEAD {t.url} failed: {type(exc).__name__}: {exc}")
                    sizes.append(None)
            products = [
                self._tile_record(
                    t.product_id, t.properties["tile_lat"], t.properties["tile_lon"], s
                )
                for t, s in zip(tiles, sizes, strict=True)
            ]
            known = [s for s in sizes if s is not None]
            estimated = sum(known) if len(known) == len(sizes) else None
            basis = f"HTTP HEAD Content-Length of {len(tiles)} whole tile(s)" + (
                ""
                if estimated is not None
                else "; at least one HEAD failed, so the total is unknown"
            )
            return self.build_plan(
                request,
                products,
                estimated_bytes=estimated,
                estimate_basis=basis,
                warnings=warnings,
            )
        rows, cols = window_shape(bounds)
        estimated = rows * cols * BYTES_PER_SAMPLE
        crop = ProductRecord(
            source=self.source,
            product_id=self.crop_product_id(request),
            product_level="GLO-30",
            url=tiles[0].url if len(tiles) == 1 else None,
            bbox_4326=bounds,
            estimated_bytes=estimated,
            licence=self.licence,
            licence_source_url=self.licence_source_url,
            properties={
                "tiles": [t.product_id for t in tiles],
                "tile_urls": [t.url for t in tiles],
                "window_bounds_4326": list(bounds),
                "window_shape": [rows, cols],
                "buffer_m": self._buffer_m(request),
            },
        )
        basis = (
            f"{rows} x {cols} px window (bbox + {self._buffer_m(request):g} m buffer, snapped "
            f"to the 1 arc-second grid) x {BYTES_PER_SAMPLE} B float32, uncompressed; read from "
            f"{len(tiles)} public COG tile(s) by HTTP range requests, written as a deflate COG"
        )
        return self.build_plan(
            request, [crop], estimated_bytes=estimated, estimate_basis=basis, warnings=warnings
        )

    @staticmethod
    def crop_product_id(request: IngestRequest) -> str:
        return f"glo30_crop_{request.aoi_id}"

    # -- DemProvider ------------------------------------------------------------------------------

    def read_window(self, bbox_4326: Bbox4326, *, buffer_m: float = 0.0) -> DemWindow:
        bounds = snap_bounds_to_grid(buffered_bbox(bbox_4326, buffer_m))
        names = tiles_for_bbox(bounds)
        datasets = [self._open(tile_url(n, self.bucket_url)) for n in names]
        opened = [d.__enter__() for d in datasets]
        try:
            crs = opened[0].crs
            data, transform = rasterio_merge(opened, bounds=bounds, nodata=None)
        finally:
            for d in datasets:
                d.__exit__(None, None, None)
        arr = np.asarray(data[0], dtype=np.float32)
        coeffs: AffineCoefficients = (
            transform.a,
            transform.b,
            transform.c,
            transform.d,
            transform.e,
            transform.f,
        )
        return DemWindow(
            data=arr,
            transform=coeffs,
            crs=str(crs),
            nodata=None,
            source=self.source,
            product_ids=tuple(names),
        )

    # -- fetch ------------------------------------------------------------------------------------

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        if "tiles" not in product.properties:  # a whole tile
            assert product.url is not None
            out = dest / f"{product.product_id}.tif"
            sha, size = self.http.stream_to(product.url, out)
            return [
                FetchedFile(
                    path=out,
                    sha256=sha,
                    size_bytes=size,
                    url=product.url,
                    params={"mode": "full_tile"},
                    product_level="GLO-30",
                )
            ]
        window = self.read_window(request.bbox_4326, buffer_m=self._buffer_m(request))
        out = dest / CROP_FILENAME
        write_cog(out, window)
        sha, size = sha256_and_size(out)
        rows, cols = window.shape
        return [
            FetchedFile(
                path=out,
                sha256=sha,
                size_bytes=size,
                url=product.url,
                params={
                    "mode": "window",
                    "tiles": list(window.product_ids),
                    "tile_urls": [tile_url(n, self.bucket_url) for n in window.product_ids],
                    "window_bounds_4326": list(window.bounds),
                    "window_shape": [rows, cols],
                    "buffer_m": self._buffer_m(request),
                    "crs": window.crs,
                },
                notes=(
                    "windowed read of public COG tile(s); float32 EPSG:4326 as delivered, "
                    "no reprojection"
                ),
                product_level="GLO-30",
            )
        ]


def write_cog(path: Path, window: DemWindow) -> None:
    """Write a `DemWindow` as a lossless (deflate, floating-point predictor) COG, no overviews."""
    rows, cols = window.shape
    a, b, c, d, e, f = window.transform
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="COG",
        dtype="float32",
        count=1,
        width=cols,
        height=rows,
        crs=window.crs,
        transform=rasterio.Affine(a, b, c, d, e, f),
        nodata=window.nodata,
        compress="deflate",
        predictor=3,
        level=9,
        blocksize=256,
        overviews="NONE",
    ) as dst:
        dst.write(window.data, 1)
        dst.update_tags(
            AREA_OR_POINT="Point",
            SERAC_SOURCE=window.source.value,
            SERAC_TILES=",".join(window.product_ids),
        )
