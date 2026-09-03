"""NISAR L-band products via `asf_search`, with the product-level constraints enforced.

The adapter lists science granules over an AOI (ancillary files filtered out), classifies each
granule as BETA or PROVISIONAL from its CMR `collectionName` (`nisar_constraints`), and
refuses to plan a fetch that would mix the two unless the request names a level explicitly.
Granules whose level cannot be established are always refused. Requests overlapping the
permanent instrument gap get a warning. Downloads need Earthdata Login; without it `fetch`
records `not_fetched` and raises (base adapter rule).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

from serac.adapters.eo._asf import (
    ASF_SEARCH_URL,
    EARTHDATA_CREDENTIAL,
    NASA_DATA_POLICY_URL,
    AsfSearchClient,
    AsfSearchLibClient,
    AsfSessionDownloader,
    EarthdataDownloader,
    bbox_wkt,
    feature_bbox,
    parse_asf_time,
)
from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo.nisar_constraints import (
    NisarLevel,
    beta_warning,
    classify_collection,
    gap_warning,
    is_science_product,
    overlaps_instrument_gap,
)
from serac.domain.manifest import DataSource
from serac.ports.ingest import CredentialSpec, DryRunPlan, IngestRequest, ProductRecord

NISAR_LICENCE = "NASA Earth science data: free and open (NASA data and information policy)"
NISAR_PLATFORM = "NISAR"
DEFAULT_PROCESSING_LEVEL = "GCOV"
DEFAULT_MAX_RESULTS = 1000
PROPERTY_KEYS: tuple[str, ...] = (
    "collectionName",
    "crid",
    "processingLevel",
    "productionConfiguration",
    "pathNumber",
    "frameNumber",
    "flightDirection",
    "orbit",
    "mainBandPolarization",
    "sideBandPolarization",
    "jointObservation",
    "frameCoverage",
    "pgeVersion",
    "fileName",
    "processingDate",
)


def feature_to_record(feature: dict[str, Any]) -> ProductRecord:
    """A `ProductRecord` for one NISAR science granule; `product_level` is the maturity."""
    props = feature["properties"]
    level = classify_collection(props.get("collectionName"), props.get("crid"))
    raw_bytes = props.get("bytes")
    return ProductRecord(
        source=DataSource.nisar_asf,
        product_id=str(props.get("sceneName") or props["fileID"]),
        product_level=level.value.upper(),
        url=props.get("url"),
        time_start=parse_asf_time(props.get("startTime")),
        time_end=parse_asf_time(props.get("stopTime")),
        bbox_4326=feature_bbox(feature),
        estimated_bytes=int(raw_bytes) if isinstance(raw_bytes, (int, float)) else None,
        licence=NISAR_LICENCE,
        licence_source_url=NASA_DATA_POLICY_URL,
        properties={k: props.get(k) for k in PROPERTY_KEYS} | {"nisar_level": level.value},
    )


def level_counts(products: list[ProductRecord]) -> Counter[str]:
    return Counter(str(p.properties.get("nisar_level")) for p in products)


class NisarAdapter(BaseIngestAdapter):
    """List and download NISAR science granules without ever mixing BETA and PROVISIONAL."""

    source: ClassVar[DataSource] = DataSource.nisar_asf
    adapter_name: ClassVar[str] = "nisar_asf"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = NISAR_LICENCE
    licence_source_url: ClassVar[str | None] = NASA_DATA_POLICY_URL
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
    def requested_level(request: IngestRequest) -> NisarLevel | None:
        """`--level beta|provisional`, or None when the caller did not choose."""
        raw = request.params.get("level")
        if raw is None:
            return None
        try:
            level = NisarLevel(str(raw).lower())
        except ValueError as exc:
            raise ValueError("level must be 'beta' or 'provisional'") from exc
        if level is NisarLevel.unknown:
            raise ValueError("level 'unknown' cannot be requested")
        return level

    @staticmethod
    def _processing_levels(request: IngestRequest) -> list[str]:
        raw = request.product_level or request.params.get("processing_level")
        if raw is None:
            return [DEFAULT_PROCESSING_LEVEL]
        return [str(raw)] if isinstance(raw, str) else [str(v) for v in raw]

    # -- port -------------------------------------------------------------------------------

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """Science granules of the requested processing level(s), all maturities, classified."""
        wanted = {lvl.upper() for lvl in self._processing_levels(request)}
        features = self.search_client.geo_search(
            intersects_with=bbox_wkt(request.bbox_4326),
            platform=[NISAR_PLATFORM],
            start=request.time_start,
            end=request.time_end,
            processing_level=sorted(wanted),
            beam_mode=None,
            flight_direction=request.params.get("flight_direction"),
            relative_orbit=None,
            max_results=int(request.params.get("max_results", DEFAULT_MAX_RESULTS)),
        )
        out: list[ProductRecord] = []
        for f in features:
            props = f.get("properties", {})
            if not is_science_product(props.get("processingLevel"), props.get("fileID")):
                continue
            if str(props.get("processingLevel", "")).upper() not in wanted:
                continue
            record = feature_to_record(f)
            if request.time_start and record.time_start and record.time_start < request.time_start:
                continue
            if request.time_end and record.time_start and record.time_start > request.time_end:
                continue
            direction = request.params.get("flight_direction")
            if direction is not None and record.properties.get("flightDirection") != direction:
                continue
            out.append(record)
        return sorted(out, key=lambda r: (r.time_start is None, r.time_start, r.product_id))

    def plan(self, request: IngestRequest) -> DryRunPlan:
        found = self.search(request)
        counts = level_counts(found)
        chosen = self.requested_level(request)
        warnings: list[str] = []
        refusals: list[str] = []
        if counts:
            warnings.append(
                "levels in the listing: "
                + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
            )
        unknown = [p for p in found if p.properties.get("nisar_level") == NisarLevel.unknown]
        if unknown:
            refusals.append(
                f"{len(unknown)} granule(s) whose level cannot be established from "
                "collectionName/crid (unknown is always refused): "
                + ", ".join(p.product_id for p in unknown[:3])
                + (" ..." if len(unknown) > 3 else "")
            )
        present = {k for k in counts if k != NisarLevel.unknown}
        if chosen is None:
            if len(present) > 1:
                refusals.append(
                    "BETA and PROVISIONAL granules both match; they are not inter-comparable. "
                    "Pass --level beta or --level provisional to choose one "
                    "(MixedProductLevelError)."
                )
            products = [p for p in found if p.properties.get("nisar_level") != NisarLevel.unknown]
        else:
            products = [p for p in found if p.properties.get("nisar_level") == chosen.value]
            if not products:
                warnings.append(f"no {chosen.value} granules match the request")
        if any(p.properties.get("nisar_level") == NisarLevel.beta for p in products):
            warnings.append(beta_warning())
        if overlaps_instrument_gap(request.time_start, request.time_end):
            warnings.append(gap_warning())
        if not found:
            warnings.append("no science granules matched (ancillary files are filtered out)")
        sizes = [p.estimated_bytes for p in products]
        known = [s for s in sizes if s is not None]
        estimated = sum(known) if known and len(known) == len(sizes) else None
        basis = (
            "sum of the ASF catalogue `properties.bytes` of every listed science granule "
            "(HDF5 files on nisar.asf.earthdatacloud.nasa.gov)"
            if estimated is not None
            else "the listing carries no `bytes` for at least one granule; size unknown"
        )
        return self.build_plan(
            request,
            products,
            estimated_bytes=estimated,
            estimate_basis=basis,
            warnings=warnings,
            refusals=refusals,
        )

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        if product.url is None:
            raise ValueError(f"{product.product_id}: no download URL in the listing")
        filename = str(product.properties.get("fileName") or f"{product.product_id}.h5")
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
                    "collectionName": product.properties.get("collectionName"),
                    "crid": product.properties.get("crid"),
                    "processingLevel": product.properties.get("processingLevel"),
                    "nisar_level": product.properties.get("nisar_level"),
                },
                notes="whole NISAR granule (HDF5) as served by ASF (Earthdata Login session)",
                product_level=product.product_level,
            )
        ]
