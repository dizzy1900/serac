"""Sentinel-2 L2A via Earth Search v1 (Element 84 STAC over the public `sentinel-cogs` bucket).

No credentials are needed. This is serac's fixture source and a secondary operational path;
CDSE is the production Sentinel-2 adapter (same `s2_cloud` selection, OAuth for downloads).

The adapter reads three bands by bbox window, at native resolution, in the item's own UTM
grid: B03 (green, 10 m, uint16), B11 (SWIR-1, 20 m, uint16) and SCL (scene classification,
20 m, uint8). The window is snapped outward to the 20 m grid so that the B03 window is
exactly twice the B11/SCL window and the three rasters stay co-registered without
resampling. NDSI = (B03 - B11) / (B03 + B11) is computed downstream, not here.

The STAC client is injected as a `Protocol` so tests feed the committed item JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import from_bounds

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import sha256_and_size
from serac.adapters.eo.s2_cloud import (
    SceneCandidate,
    class_histogram,
    cloud_fraction,
    collapse_reprocessings,
    select_scenes,
)
from serac.domain.manifest import DataSource
from serac.ports.ingest import Bbox4326, DryRunPlan, IngestRequest, ProductRecord

EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1"
S2_L2A_COLLECTION = "sentinel-2-l2a"
SENTINEL_LICENCE = (
    "Copernicus Sentinel data: free, full and open access under the Legal Notice on the use "
    "of Copernicus Sentinel Data and Service Information (attribution 'Contains modified "
    "Copernicus Sentinel data [year]')"
)
SENTINEL_LICENCE_URL = (
    "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice"
)
BAND_ASSETS: dict[str, str] = {"B03": "green", "B11": "swir16", "SCL": "scl"}
"""File stem -> Earth Search asset key."""
BAND_RESOLUTION_M: dict[str, int] = {"B03": 10, "B11": 20, "SCL": 20}
BAND_BYTES_PER_SAMPLE: dict[str, int] = {"B03": 2, "B11": 2, "SCL": 1}
BAND_DTYPE: dict[str, str] = {"B03": "uint16", "B11": "uint16", "SCL": "uint8"}
SNAP_M = 20
DEFAULT_MAX_CLOUD_PERCENT = 40.0
DEFAULT_SEARCH_LIMIT = 200
ITEM_FILENAME = "item.json"

RasterOpener = Callable[[str], AbstractContextManager[Any]]


class StacSearchClient(Protocol):
    """The one call the adapter makes against a STAC API; fakes return recorded item dicts."""

    def search_items(
        self,
        *,
        collection: str,
        bbox: Bbox4326,
        datetime_range: str,
        max_cloud: float | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class PystacSearchClient:
    """`StacSearchClient` over `pystac_client`; the production choice."""

    def __init__(self, url: str = EARTH_SEARCH_URL) -> None:
        self.url = url
        self._client: Any = None

    def _open(self) -> Any:
        if self._client is None:
            from pystac_client import Client

            self._client = Client.open(self.url)
        return self._client

    def search_items(
        self,
        *,
        collection: str,
        bbox: Bbox4326,
        datetime_range: str,
        max_cloud: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = {"eo:cloud_cover": {"lte": max_cloud}} if max_cloud is not None else None
        search = self._open().search(
            collections=[collection],
            bbox=list(bbox),
            datetime=datetime_range,
            query=query,
            max_items=limit,
        )
        items: list[dict[str, Any]] = list(search.items_as_dicts())
        return items


def _default_opener(url: str) -> AbstractContextManager[Any]:
    return rasterio.open(url)  # type: ignore[no-any-return]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def utm_bounds(bbox: Bbox4326, epsg: int) -> tuple[float, float, float, float]:
    """Envelope of `bbox` corners in `epsg` (metres)."""
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    w, s, e, n = bbox
    xs, ys = tf.transform([w, e, e, w], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


def snap_bounds(
    bounds: tuple[float, float, float, float],
    origin: tuple[float, float],
    step: float = SNAP_M,
) -> tuple[float, float, float, float]:
    """Grow `bounds` outward to multiples of `step` from the raster `origin` (x0, y0)."""
    import math

    x0, y0 = origin
    w, s, e, n = bounds
    west = x0 + math.floor((w - x0) / step) * step
    east = x0 + math.ceil((e - x0) / step) * step
    north = y0 - math.floor((y0 - n) / step) * step
    south = y0 - math.ceil((y0 - s) / step) * step
    return west, south, east, north


def window_pixels(bounds: tuple[float, float, float, float], resolution_m: int) -> tuple[int, int]:
    w, s, e, n = bounds
    return round((n - s) / resolution_m), round((e - w) / resolution_m)


def item_to_candidate(item: dict[str, Any]) -> SceneCandidate:
    props = item["properties"]
    return SceneCandidate(
        product_id=str(item["id"]),
        acquired=datetime.fromisoformat(str(props["datetime"]).replace("Z", "+00:00")),
        tile_cloud_cover=props.get("eo:cloud_cover"),
        processing_baseline=props.get("s2:processing_baseline"),
    )


class EarthSearchSentinel2Adapter(BaseIngestAdapter):
    """Cloud-aware Sentinel-2 L2A windows (B03, B11, SCL) from public COGs."""

    source: ClassVar[DataSource] = DataSource.sentinel2_earthsearch
    adapter_name: ClassVar[str] = "sentinel2_earthsearch"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = SENTINEL_LICENCE
    licence_source_url: ClassVar[str | None] = SENTINEL_LICENCE_URL

    def __init__(
        self,
        stac: StacSearchClient,
        *,
        raster_opener: RasterOpener | None = None,
        collection: str = S2_L2A_COLLECTION,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.stac = stac
        self.collection = collection
        self._open = raster_opener or _default_opener

    # -- request parameters -----------------------------------------------------------------

    @staticmethod
    def _max_cloud(request: IngestRequest) -> float | None:
        value = request.params.get("max_cloud", DEFAULT_MAX_CLOUD_PERCENT)
        return None if value is None else float(value)

    @staticmethod
    def _window_override(request: IngestRequest) -> tuple[float, float, float, float] | None:
        """`params["window_bounds"]`: explicit (w, s, e, n) in the item CRS, metres.

        Used when an exactly reproducible window matters more than the bbox (fixtures).
        The bounds are still snapped outward to the 20 m grid of the SCL raster.
        """
        raw = request.params.get("window_bounds")
        if raw is None:
            return None
        w, s, e, n = (float(v) for v in raw)
        return (w, s, e, n)

    @staticmethod
    def _max_scenes(request: IngestRequest) -> int | None:
        value = request.params.get("max_scenes")
        return None if value is None else int(value)

    # -- port: search / plan -----------------------------------------------------------------

    def _record_from_item(self, item: dict[str, Any]) -> ProductRecord:
        props = item["properties"]
        acquired = datetime.fromisoformat(str(props["datetime"]).replace("Z", "+00:00"))
        assets = {
            stem: str(item["assets"][key]["href"])
            for stem, key in BAND_ASSETS.items()
            if key in item.get("assets", {})
        }
        self_href = next(
            (link["href"] for link in item.get("links", []) if link.get("rel") == "self"), None
        )
        bbox_raw = item.get("bbox")
        bbox = tuple(float(v) for v in bbox_raw) if bbox_raw is not None else None
        return ProductRecord(
            source=self.source,
            product_id=str(item["id"]),
            product_level="L2A",
            url=self_href,
            assets=assets,
            time_start=acquired,
            time_end=acquired,
            bbox_4326=bbox,  # type: ignore[arg-type]
            licence=self.licence,
            licence_source_url=self.licence_source_url,
            properties={
                "eo:cloud_cover": props.get("eo:cloud_cover"),
                "s2:processing_baseline": props.get("s2:processing_baseline"),
                "proj:epsg": props.get("proj:epsg"),
                "platform": props.get("platform"),
                "stac_item": item,
            },
        )

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """STAC search with the tile-level cloud filter; duplicates of an acquisition collapsed."""
        if request.time_start is None or request.time_end is None:
            raise ValueError("Sentinel-2 search needs time_start and time_end")
        items = self.stac.search_items(
            collection=self.collection,
            bbox=request.bbox_4326,
            datetime_range=f"{_iso(request.time_start)}/{_iso(request.time_end)}",
            max_cloud=self._max_cloud(request),
            limit=int(request.params.get("limit", DEFAULT_SEARCH_LIMIT)),
        )
        by_id = {str(item["id"]): item for item in items}
        candidates = [item_to_candidate(item) for item in items]
        if not request.params.get("keep_reprocessings", False):
            candidates = collapse_reprocessings(candidates)
        max_scenes = self._max_scenes(request)
        if max_scenes is not None:
            candidates = select_scenes(candidates, n=max_scenes)
        return [self._record_from_item(by_id[c.product_id]) for c in candidates]

    def _bands_for(self, product: ProductRecord) -> list[str]:
        return [stem for stem in BAND_ASSETS if stem in product.assets]

    def estimate_product_bytes(
        self,
        product: ProductRecord,
        bbox: Bbox4326,
        window_override: tuple[float, float, float, float] | None = None,
    ) -> int | None:
        epsg = product.properties.get("proj:epsg")
        if epsg is None:
            return None
        raw_bounds = window_override or utm_bounds(bbox, int(epsg))
        bounds = snap_bounds(raw_bounds, origin=(0.0, 0.0))
        total = 0
        for stem in self._bands_for(product):
            rows, cols = window_pixels(bounds, BAND_RESOLUTION_M[stem])
            total += rows * cols * BAND_BYTES_PER_SAMPLE[stem]
        total += len(json.dumps(product.properties.get("stac_item", {})))
        return total

    def plan(self, request: IngestRequest) -> DryRunPlan:
        found = self.search(request)
        products: list[ProductRecord] = []
        warnings: list[str] = []
        for p in found:
            est = self.estimate_product_bytes(p, request.bbox_4326, self._window_override(request))
            if est is None:
                warnings.append(f"{p.product_id}: no proj:epsg in item; size unknown")
            products.append(p.model_copy(update={"estimated_bytes": est}))
        sizes = [p.estimated_bytes for p in products]
        estimated = (
            sum(s for s in sizes if s is not None) if all(s is not None for s in sizes) else None
        )
        basis = (
            "pixels of the bbox window at native resolution (B03 10 m uint16, B11 20 m uint16, "
            "SCL 20 m uint8; window snapped outward to the 20 m grid) x bytes per sample, "
            "uncompressed, plus the STAC item JSON; per scene"
        )
        if not products:
            warnings.append("no scenes matched the search")
        return self.build_plan(
            request, products, estimated_bytes=estimated, estimate_basis=basis, warnings=warnings
        )

    # -- fetch ------------------------------------------------------------------------------

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        item = product.properties.get("stac_item")
        if not isinstance(item, dict):
            raise ValueError(f"{product.product_id}: product carries no STAC item")
        files: list[FetchedFile] = []
        item_path = dest / ITEM_FILENAME
        item_path.write_text(json.dumps(item, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        sha, size = sha256_and_size(item_path)
        files.append(
            FetchedFile(
                path=item_path,
                sha256=sha,
                size_bytes=size,
                url=product.url,
                params={"kind": "stac_item"},
                notes="STAC item as returned by Earth Search (verbatim, keys sorted)",
                product_level="L2A",
            )
        )
        bands = self._bands_for(product)
        if "SCL" not in bands:
            raise ValueError(f"{product.product_id}: no SCL asset; cannot assess cloud")
        # SCL first: its 20 m grid defines the snapped window for every band.
        scl_href = product.assets["SCL"]
        with self._open(scl_href) as scl_ds:
            epsg = int(scl_ds.crs.to_epsg())
            origin = (float(scl_ds.transform.c), float(scl_ds.transform.f))
            raw_bounds = self._window_override(request) or utm_bounds(request.bbox_4326, epsg)
            bounds = snap_bounds(raw_bounds, origin)
        aoi_fraction: float | None = None
        histogram: dict[str, int] = {}
        for stem in ("SCL", *[b for b in bands if b != "SCL"]):
            href = product.assets[stem]
            out = dest / f"{stem}.tif"
            arr, transform, crs = self._read_window(href, bounds)
            if stem == "SCL":
                aoi_fraction = cloud_fraction(arr)
                histogram = class_histogram(arr)
            _write_band_cog(out, arr, transform, crs, BAND_DTYPE[stem])
            sha, size = sha256_and_size(out)
            rows, cols = arr.shape
            files.append(
                FetchedFile(
                    path=out,
                    sha256=sha,
                    size_bytes=size,
                    url=href,
                    params={
                        "kind": "band",
                        "band": stem,
                        "resolution_m": BAND_RESOLUTION_M[stem],
                        "window_bounds_epsg": epsg,
                        "window_bounds": list(bounds),
                        "window_shape": [rows, cols],
                        "aoi_cloud_fraction": aoi_fraction,
                        "eo:cloud_cover": product.properties.get("eo:cloud_cover"),
                        **({"scl_histogram": histogram} if stem == "SCL" else {}),
                    },
                    notes=(
                        "windowed read of the public COG at native resolution, item CRS, "
                        "no resampling"
                    ),
                    product_level="L2A",
                )
            )
        return files

    def _read_window(
        self, href: str, bounds: tuple[float, float, float, float]
    ) -> tuple[Any, Any, Any]:
        with self._open(href) as ds:
            win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
            arr = ds.read(1, window=win)
            return arr, ds.window_transform(win), ds.crs

    def read_scl_window(
        self,
        product: ProductRecord,
        bbox: Bbox4326,
        *,
        window_bounds: tuple[float, float, float, float] | None = None,
    ) -> Any:
        """The SCL array over `bbox` (or explicit `window_bounds` in the item CRS); one read."""
        href = product.assets.get("SCL")
        if href is None:
            return None
        with self._open(href) as ds:
            epsg = int(ds.crs.to_epsg())
            origin = (float(ds.transform.c), float(ds.transform.f))
            bounds = snap_bounds(window_bounds or utm_bounds(bbox, epsg), origin)
            win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
            return ds.read(1, window=win)

    def aoi_cloud_fraction(
        self,
        product: ProductRecord,
        bbox: Bbox4326,
        *,
        window_bounds: tuple[float, float, float, float] | None = None,
    ) -> float | None:
        """Fraction of flagged SCL pixels over the AOI window (network: one windowed read)."""
        scl = self.read_scl_window(product, bbox, window_bounds=window_bounds)
        return None if scl is None else cloud_fraction(scl)


def _write_band_cog(path: Path, arr: Any, transform: Any, crs: Any, dtype: str) -> None:
    data = np.asarray(arr).astype(dtype, copy=False)
    with rasterio.open(
        path,
        "w",
        driver="COG",
        dtype=dtype,
        count=1,
        width=data.shape[1],
        height=data.shape[0],
        crs=crs,
        transform=transform,
        nodata=0,
        compress="deflate",
        predictor=2,
        level=9,
        blocksize=256,
        overviews="NONE",
    ) as dst:
        dst.write(data, 1)
