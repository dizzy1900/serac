"""Sentinel-2 L2A via the Copernicus Data Space Ecosystem (the production path, ADR-0006).

Search goes through the public CDSE STAC API (`stac.dataspace.copernicus.eu/v1`) behind the
same `StacSearchClient` Protocol as the Earth Search adapter, and scene selection is shared
(`s2_cloud`). Asset reads need an OAuth 2 client-credentials token from
`identity.dataspace.copernicus.eu`; the token flow lives behind `CdseTokenProvider` so tests
use a fake, and the JP2 assets are read by window (B03 10 m, B11 20 m, SCL 20 m) through
GDAL's `/vsicurl/` with a bearer header. Without `CDSE_CLIENT_ID/SECRET`, `fetch` records
`not_fetched` for every scene and raises (base adapter rule).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx
import rasterio

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import make_httpx_client, sha256_and_size
from serac.adapters.eo.earthsearch_sentinel2 import (
    BAND_BYTES_PER_SAMPLE,
    BAND_DTYPE,
    BAND_RESOLUTION_M,
    DEFAULT_MAX_CLOUD_PERCENT,
    DEFAULT_SEARCH_LIMIT,
    ITEM_FILENAME,
    SENTINEL_LICENCE,
    SENTINEL_LICENCE_URL,
    StacSearchClient,
    item_to_candidate,
    snap_bounds,
    utm_bounds,
    window_pixels,
    write_band_cog,
)
from serac.adapters.eo.s2_cloud import (
    SceneCandidate,
    class_histogram,
    cloud_fraction,
    collapse_reprocessings,
    select_scenes,
)
from serac.domain.manifest import DataSource
from serac.ports.ingest import Bbox4326, CredentialSpec, DryRunPlan, IngestRequest, ProductRecord

CDSE_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
)
CDSE_S2_L2A_COLLECTION = "sentinel-2-l2a"
CDSE_BAND_ASSETS: dict[str, str] = {"B03": "B03_10m", "B11": "B11_20m", "SCL": "SCL_20m"}
"""File stem -> CDSE STAC asset key (the `alternate.https.href` is the OData download URL)."""
TOKEN_SAFETY_MARGIN_S = 60.0

CDSE_CREDENTIAL = CredentialSpec(
    name="CDSE OAuth client credentials",
    env_vars=("CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET"),
    purpose="read Sentinel-2 L2A assets from the Copernicus Data Space Ecosystem",
)

AuthedRasterOpener = Callable[[str, str], AbstractContextManager[Any]]
"""Opens (href, bearer token) as a context manager yielding a rasterio dataset."""


class CdseTokenProvider(Protocol):
    def token(self) -> str: ...


class TokenHttp(Protocol):
    """POST an `application/x-www-form-urlencoded` body and decode the JSON reply."""

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]: ...


class HttpxTokenHttp:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_httpx_client(timeout_s=30.0)

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        response = self._client.post(url, data=data)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


class CdseOAuthClient:
    """OAuth 2 client-credentials grant against the CDSE identity service; caches the token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: TokenHttp | None = None,
        token_url: str = CDSE_TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or HttpxTokenHttp()
        self._token_url = token_url
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self.requests = 0

    def token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._expires_at:
            return self._token
        body = self._http.post_form(
            self._token_url,
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        self.requests += 1
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("CDSE token response carries no access_token")
        ttl = float(body.get("expires_in", 0))
        self._token = token
        self._expires_at = now + max(ttl - TOKEN_SAFETY_MARGIN_S, 0.0)
        return token


def _default_opener(href: str, token: str) -> AbstractContextManager[Any]:
    """`/vsicurl/` with a bearer header; GDAL streams only the requested window."""
    env = rasterio.Env(
        GDAL_HTTP_HEADERS=f"Authorization: Bearer {token}",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    )
    env.__enter__()
    try:
        ds = rasterio.open(f"/vsicurl/{href}")
    except BaseException:
        env.__exit__(None, None, None)
        raise

    class _Both:
        def __enter__(self) -> Any:
            return ds.__enter__()

        def __exit__(self, *exc: object) -> None:
            try:
                ds.__exit__(*exc)
            finally:
                env.__exit__(*exc)

    return _Both()


def asset_https_href(item: dict[str, Any], key: str) -> str | None:
    """CDSE lists S3 hrefs first; the HTTPS (OData) href sits under `alternate.https`."""
    asset = item.get("assets", {}).get(key)
    if asset is None:
        return None
    alt = asset.get("alternate", {}).get("https", {})
    href = alt.get("href") or asset.get("href")
    if isinstance(href, str) and href.startswith("https://"):
        return href
    return None


def item_epsg(item: dict[str, Any]) -> int | None:
    """`proj:epsg` / `proj:code` from the item properties, else from the SCL asset (CDSE)."""
    candidates: list[dict[str, Any]] = [item.get("properties", {})]
    candidates.extend(item.get("assets", {}).get(k, {}) for k in CDSE_BAND_ASSETS.values())
    for source in candidates:
        if source.get("proj:epsg") is not None:
            return int(source["proj:epsg"])
        code = source.get("proj:code")
        if isinstance(code, str) and code.upper().startswith("EPSG:"):
            return int(code.split(":")[-1])
    return None


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def cdse_item_to_candidate(item: dict[str, Any]) -> SceneCandidate:
    """CDSE stores the processing baseline under `processing:version`."""
    base = item_to_candidate(item)
    baseline = item["properties"].get("processing:version")
    if base.processing_baseline is None and baseline is not None:
        return replace(base, processing_baseline=str(baseline))
    return base


class CdseSentinel2Adapter(BaseIngestAdapter):
    """Cloud-aware Sentinel-2 L2A windows (B03, B11, SCL) from CDSE, OAuth-authenticated."""

    source: ClassVar[DataSource] = DataSource.sentinel2_cdse
    adapter_name: ClassVar[str] = "sentinel2_cdse"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = SENTINEL_LICENCE
    licence_source_url: ClassVar[str | None] = SENTINEL_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (CDSE_CREDENTIAL,)

    def __init__(
        self,
        stac: StacSearchClient,
        *,
        token_provider: CdseTokenProvider | None = None,
        raster_opener: AuthedRasterOpener | None = None,
        collection: str = CDSE_S2_L2A_COLLECTION,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.stac = stac
        self.collection = collection
        self._tokens = token_provider
        self._open = raster_opener or _default_opener

    @property
    def tokens(self) -> CdseTokenProvider:
        if self._tokens is None:
            cid, secret = self.settings.cdse_client_id, self.settings.cdse_client_secret
            if cid is None or secret is None:
                raise RuntimeError("CDSE credentials missing; fetch() should have refused")
            self._tokens = CdseOAuthClient(cid.get_secret_value(), secret.get_secret_value())
        return self._tokens

    # -- request parameters -----------------------------------------------------------------

    @staticmethod
    def _max_cloud(request: IngestRequest) -> float | None:
        value = request.params.get("max_cloud", DEFAULT_MAX_CLOUD_PERCENT)
        return None if value is None else float(value)

    @staticmethod
    def _window_override(request: IngestRequest) -> tuple[float, float, float, float] | None:
        raw = request.params.get("window_bounds")
        if raw is None:
            return None
        w, s, e, n = (float(v) for v in raw)
        return (w, s, e, n)

    # -- search / plan ----------------------------------------------------------------------

    def _record_from_item(self, item: dict[str, Any]) -> ProductRecord:
        props = item["properties"]
        acquired = datetime.fromisoformat(str(props["datetime"]).replace("Z", "+00:00"))
        assets = {
            stem: href
            for stem, key in CDSE_BAND_ASSETS.items()
            if (href := asset_https_href(item, key)) is not None
        }
        self_href = next(
            (link["href"] for link in item.get("links", []) if link.get("rel") == "self"), None
        )
        bbox_raw = item.get("bbox")
        bbox = tuple(float(v) for v in bbox_raw) if bbox_raw is not None else None
        epsg = item_epsg(item)
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
                "eo:snow_cover": props.get("eo:snow_cover"),
                "s2:processing_baseline": props.get("processing:version"),
                "proj:epsg": epsg,
                "platform": props.get("platform"),
                "product_size": (props.get("_private") or {}).get("product_size"),
                "stac_item": item,
            },
        )

    def search(self, request: IngestRequest) -> list[ProductRecord]:
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
        candidates = [cdse_item_to_candidate(item) for item in items]
        if not request.params.get("keep_reprocessings", False):
            candidates = collapse_reprocessings(candidates)
        max_scenes = request.params.get("max_scenes")
        if max_scenes is not None:
            candidates = select_scenes(candidates, n=int(max_scenes))
        return [self._record_from_item(by_id[c.product_id]) for c in candidates]

    def estimate_product_bytes(
        self, product: ProductRecord, bbox: Bbox4326, window: tuple[float, ...] | None
    ) -> int | None:
        epsg = product.properties.get("proj:epsg")
        if epsg is None:
            return None
        raw_bounds = window or utm_bounds(bbox, int(epsg))
        w, s, e, n = raw_bounds
        bounds = snap_bounds((w, s, e, n), origin=(0.0, 0.0))
        total = 0
        for stem in product.assets:
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
                warnings.append(f"{p.product_id}: no proj:epsg/proj:code in item; size unknown")
            if len(p.assets) < 3:
                missing = sorted(set(CDSE_BAND_ASSETS) - set(p.assets))
                warnings.append(f"{p.product_id}: missing HTTPS asset(s) for {missing}")
            products.append(p.model_copy(update={"estimated_bytes": est}))
        sizes = [p.estimated_bytes for p in products]
        estimated = (
            sum(s for s in sizes if s is not None) if all(s is not None for s in sizes) else None
        )
        if not products:
            warnings.append("no scenes matched the search")
        warnings.append(
            "CDSE assets are JP2 read by window over HTTPS with a bearer token; the estimate is "
            "the uncompressed window, the transfer is JP2 tiles covering it"
        )
        return self.build_plan(
            request,
            products,
            estimated_bytes=estimated,
            estimate_basis=(
                "pixels of the bbox window at native resolution (B03 10 m uint16, B11 20 m "
                "uint16, SCL 20 m uint8; window snapped outward to the 20 m grid) x bytes per "
                "sample, uncompressed, plus the STAC item JSON; per scene"
            ),
            warnings=warnings,
        )

    # -- fetch ------------------------------------------------------------------------------

    def _read_window(
        self, href: str, bounds: tuple[float, float, float, float]
    ) -> tuple[Any, Any, Any]:
        from rasterio.windows import from_bounds

        with self._open(href, self.tokens.token()) as ds:
            win = from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()
            arr = ds.read(1, window=win)
            return arr, ds.window_transform(win), ds.crs

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        item = product.properties.get("stac_item")
        if not isinstance(item, dict):
            raise ValueError(f"{product.product_id}: product carries no STAC item")
        if "SCL" not in product.assets:
            raise ValueError(f"{product.product_id}: no SCL asset; cannot assess cloud")
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
                notes="STAC item as returned by the CDSE STAC API (verbatim, keys sorted)",
                product_level="L2A",
            )
        )
        with self._open(product.assets["SCL"], self.tokens.token()) as scl_ds:
            epsg = int(scl_ds.crs.to_epsg())
            origin = (float(scl_ds.transform.c), float(scl_ds.transform.f))
            raw_bounds = self._window_override(request) or utm_bounds(request.bbox_4326, epsg)
            bounds = snap_bounds(raw_bounds, origin)
        aoi_fraction: float | None = None
        histogram: dict[str, int] = {}
        for stem in ("SCL", *[b for b in product.assets if b != "SCL"]):
            href = product.assets[stem]
            out = dest / f"{stem}.tif"
            arr, transform, crs = self._read_window(href, bounds)
            if stem == "SCL":
                aoi_fraction = cloud_fraction(arr)
                histogram = class_histogram(arr)
            write_band_cog(out, arr, transform, crs, BAND_DTYPE[stem])
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
                        "windowed read of the CDSE JP2 asset at native resolution, item CRS, "
                        "no resampling (OAuth bearer token)"
                    ),
                    product_level="L2A",
                )
            )
        return files
