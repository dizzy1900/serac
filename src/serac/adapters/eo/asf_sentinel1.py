"""Sentinel-1 listing and download via ASF (`asf_search` geo_search; Earthdata for bytes).

Search is public and returns one GeoJSON feature per granule. The adapter lists IW SLC (the
InSAR input) or GRD_HD, groups granules by relative orbit (`pathNumber`) so the HyP3 pair
planner can pick same-track pairs, and estimates a fetch from the catalogue's own `bytes`
field. Downloading needs Earthdata Login: without it `fetch` records `not_fetched` for every
granule and raises `CredentialsMissingError` (the base adapter's rule).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from serac.adapters.eo._asf import (
    ASF_SEARCH_URL,
    EARTHDATA_CREDENTIAL,
    AsfSearchClient,
    AsfSearchLibClient,
    AsfSessionDownloader,
    EarthdataDownloader,
    bbox_wkt,
    feature_bbox,
    parse_asf_time,
)
from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo.earthsearch_sentinel2 import SENTINEL_LICENCE, SENTINEL_LICENCE_URL
from serac.domain.manifest import DataSource
from serac.ports.ingest import CredentialSpec, DryRunPlan, IngestRequest, ProductRecord

PLATFORMS: tuple[str, ...] = ("Sentinel-1A", "Sentinel-1B", "Sentinel-1C")
PROCESSING_LEVELS: frozenset[str] = frozenset({"SLC", "GRD_HD", "GRD_MD", "GRD_HS"})
DEFAULT_PROCESSING_LEVEL = "SLC"
DEFAULT_BEAM_MODE = "IW"
DEFAULT_MAX_RESULTS = 500
PROPERTY_KEYS: tuple[str, ...] = (
    "pathNumber",
    "frameNumber",
    "flightDirection",
    "orbit",
    "platform",
    "polarization",
    "beamModeType",
    "processingLevel",
    "groupID",
    "fileName",
    "md5sum",
    "processingDate",
)


def feature_to_record(feature: dict[str, Any], licence: str, licence_url: str) -> ProductRecord:
    """A `ProductRecord` from one ASF GeoJSON feature (Sentinel-1 granule)."""
    props = feature["properties"]
    raw_bytes = props.get("bytes")
    return ProductRecord(
        source=DataSource.sentinel1_asf,
        product_id=str(props["sceneName"]),
        product_level=props.get("processingLevel"),
        url=props.get("url"),
        time_start=parse_asf_time(props.get("startTime")),
        time_end=parse_asf_time(props.get("stopTime")),
        bbox_4326=feature_bbox(feature),
        estimated_bytes=int(raw_bytes) if isinstance(raw_bytes, (int, float)) else None,
        licence=licence,
        licence_source_url=licence_url,
        properties={k: props.get(k) for k in PROPERTY_KEYS},
    )


def group_by_relative_orbit(products: Sequence[ProductRecord]) -> dict[int, list[ProductRecord]]:
    """Granules per `pathNumber`, each list ordered by acquisition time."""
    groups: dict[int, list[ProductRecord]] = defaultdict(list)
    for p in products:
        path = p.properties.get("pathNumber")
        if path is None:
            continue
        groups[int(path)].append(p)
    return {
        path: sorted(items, key=lambda p: (p.time_start is None, p.time_start))
        for path, items in sorted(groups.items())
    }


class Sentinel1AsfAdapter(BaseIngestAdapter):
    """List Sentinel-1 IW granules over an AOI; download them with Earthdata Login."""

    source: ClassVar[DataSource] = DataSource.sentinel1_asf
    adapter_name: ClassVar[str] = "sentinel1_asf"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = SENTINEL_LICENCE
    licence_source_url: ClassVar[str | None] = SENTINEL_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (EARTHDATA_CREDENTIAL,)

    def __init__(
        self,
        search_client: AsfSearchClient | None = None,
        *,
        downloader: EarthdataDownloader | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.search_client: AsfSearchClient = search_client or AsfSearchLibClient()
        self._downloader = downloader

    @property
    def downloader(self) -> EarthdataDownloader:
        """Built lazily from settings so a missing credential never reaches this line."""
        if self._downloader is None:
            user = self.settings.earthdata_username
            password = self.settings.earthdata_password
            if user is None or password is None:
                raise RuntimeError("Earthdata credentials missing; fetch() should have refused")
            self._downloader = AsfSessionDownloader(
                user.get_secret_value(), password.get_secret_value()
            )
        return self._downloader

    # -- request parameters -----------------------------------------------------------------

    @staticmethod
    def _processing_level(request: IngestRequest) -> str:
        level = str(request.product_level or request.params.get("processing_level") or "")
        level = level or DEFAULT_PROCESSING_LEVEL
        if level not in PROCESSING_LEVELS:
            raise ValueError(f"processing_level must be one of {sorted(PROCESSING_LEVELS)}")
        return level

    @staticmethod
    def _relative_orbits(request: IngestRequest) -> list[int] | None:
        raw = request.params.get("relative_orbit")
        if raw is None:
            return None
        return [int(raw)] if isinstance(raw, (int, str)) else [int(v) for v in raw]

    # -- port ----------------------------------------------------------------------------------

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        level = self._processing_level(request)
        features = self.search_client.geo_search(
            intersects_with=bbox_wkt(request.bbox_4326),
            platform=PLATFORMS,
            start=request.time_start,
            end=request.time_end,
            processing_level=[level],
            beam_mode=[str(request.params.get("beam_mode", DEFAULT_BEAM_MODE))],
            flight_direction=request.params.get("flight_direction"),
            relative_orbit=self._relative_orbits(request),
            max_results=int(request.params.get("max_results", DEFAULT_MAX_RESULTS)),
        )
        records = [
            feature_to_record(f, self.licence, self.licence_source_url or "") for f in features
        ]
        # A recorded listing may hold more than the request asked for; filter it the same way.
        beam = str(request.params.get("beam_mode", DEFAULT_BEAM_MODE))
        orbits = self._relative_orbits(request)
        direction = request.params.get("flight_direction")
        out: list[ProductRecord] = []
        for r in records:
            if r.product_level != level or r.properties.get("beamModeType") != beam:
                continue
            if orbits is not None and int(r.properties.get("pathNumber") or -1) not in orbits:
                continue
            if direction is not None and r.properties.get("flightDirection") != direction:
                continue
            if request.time_start and r.time_start and r.time_start < request.time_start:
                continue
            if request.time_end and r.time_start and r.time_start > request.time_end:
                continue
            out.append(r)
        return sorted(out, key=lambda r: (r.time_start is None, r.time_start, r.product_id))

    def plan(self, request: IngestRequest) -> DryRunPlan:
        products = self.search(request)
        sizes = [p.estimated_bytes for p in products]
        known = [s for s in sizes if s is not None]
        estimated = sum(known) if known and len(known) == len(sizes) else None
        warnings: list[str] = []
        if not products:
            warnings.append("no granules matched the search")
        if len(known) != len(sizes):
            warnings.append(
                f"{len(sizes) - len(known)} granule(s) carry no `bytes` in the catalogue"
            )
        groups = group_by_relative_orbit(products)
        if groups:
            summary = ", ".join(f"path {k}: {len(v)}" for k, v in groups.items())
            warnings.append(f"relative orbits: {summary}")
        basis = (
            "sum of the ASF catalogue `properties.bytes` of every listed granule "
            f"({self._processing_level(request)} zip archives as served by datapool.asf.alaska.edu)"
        )
        return self.build_plan(
            request, products, estimated_bytes=estimated, estimate_basis=basis, warnings=warnings
        )

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        if product.url is None:
            raise ValueError(f"{product.product_id}: no download URL in the listing")
        filename = str(product.properties.get("fileName") or f"{product.product_id}.zip")
        out = dest / filename
        sha, size = self.downloader.download(product.url, out)
        return [
            FetchedFile(
                path=out,
                sha256=sha,
                size_bytes=size,
                url=product.url,
                params={
                    "search_url": ASF_SEARCH_URL,
                    "pathNumber": product.properties.get("pathNumber"),
                    "flightDirection": product.properties.get("flightDirection"),
                    "catalogue_md5": product.properties.get("md5sum"),
                },
                notes="whole granule archive as served by ASF (Earthdata Login session)",
                product_level=product.product_level,
            )
        ]
