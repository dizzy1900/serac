"""ERA5 reanalysis via the Copernicus Climate Data Store (`cdsapi`).

One request per AOI and window: hourly single-level fields (default `2m_temperature`) over the
0.25 deg cells that cover the bbox, delivered as NetCDF. The CDS client sits behind a
`CdsClient` Protocol so tests use a fake; the real client needs `CDSAPI_KEY` (free account,
dataset licence accepted once on the CDS site). Without a key `fetch` records `not_fetched`
and raises (base adapter rule). The plan estimate is arithmetic on the request, not a guess:
cells x hourly steps x variables x 4 B (float32, uncompressed).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Protocol

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import sha256_and_size
from serac.domain.manifest import DataSource
from serac.ports.ingest import Bbox4326, CredentialSpec, DryRunPlan, IngestRequest, ProductRecord

ERA5_DATASET = "reanalysis-era5-single-levels"
ERA5_GRID_DEG = 0.25
ERA5_LICENCE = (
    "ERA5 (Copernicus Climate Change Service, C3S): Licence to use Copernicus Products, "
    "accepted per dataset on the CDS site; attribution 'Contains modified Copernicus Climate "
    "Change Service information [year]'"
)
ERA5_LICENCE_URL = f"https://cds.climate.copernicus.eu/datasets/{ERA5_DATASET}?tab=overview"
DEFAULT_VARIABLES: tuple[str, ...] = ("2m_temperature",)
HOURS: tuple[str, ...] = tuple(f"{h:02d}:00" for h in range(24))
BYTES_PER_VALUE = 4
OUTPUT_FILENAME = "era5.nc"

CDS_CREDENTIAL = CredentialSpec(
    name="CDS API key",
    env_vars=("CDSAPI_KEY",),
    purpose="retrieve ERA5 fields from the Copernicus Climate Data Store",
)


class CdsClient(Protocol):
    """The one call the adapter makes; `retrieve` writes the dataset to `target`."""

    def retrieve(self, dataset: str, request: dict[str, Any], target: Path) -> None: ...


class CdsapiClient:
    """`CdsClient` over `cdsapi.Client`; the production choice."""

    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key
        self._client: Any = None

    def _open(self) -> Any:
        if self._client is None:
            import cdsapi

            self._client = cdsapi.Client(url=self._url, key=self._key, quiet=True)
        return self._client

    def retrieve(self, dataset: str, request: dict[str, Any], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._open().retrieve(dataset, request, str(target))


def era5_area(bbox: Bbox4326) -> tuple[float, float, float, float]:
    """CDS `area` = [north, west, south, east], grown outward to the 0.25 deg grid."""
    w, s, e, n = bbox
    g = ERA5_GRID_DEG
    return (
        math.ceil(n / g) * g,
        math.floor(w / g) * g,
        math.floor(s / g) * g,
        math.ceil(e / g) * g,
    )


def era5_cells(area: tuple[float, float, float, float]) -> tuple[int, int]:
    """(rows, cols) of 0.25 deg grid points covering `area` (both edges inclusive)."""
    north, west, south, east = area
    rows = round((north - south) / ERA5_GRID_DEG) + 1
    cols = round((east - west) / ERA5_GRID_DEG) + 1
    return rows, cols


def date_list(start: datetime, end: datetime) -> list[datetime]:
    d0 = start.astimezone(UTC).date()
    d1 = end.astimezone(UTC).date()
    return [
        datetime.combine(d0 + timedelta(days=i), datetime.min.time(), tzinfo=UTC)
        for i in range((d1 - d0).days + 1)
    ]


def build_cds_request(
    bbox: Bbox4326, start: datetime, end: datetime, variables: tuple[str, ...]
) -> dict[str, Any]:
    """The literal request body sent to CDS (also what the ledger records)."""
    days = date_list(start, end)
    years = sorted({f"{d:%Y}" for d in days})
    months = sorted({f"{d:%m}" for d in days})
    day_numbers = sorted({f"{d:%d}" for d in days})
    return {
        "product_type": ["reanalysis"],
        "variable": list(variables),
        "year": years,
        "month": months,
        "day": day_numbers,
        "time": list(HOURS),
        "area": list(era5_area(bbox)),
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


class Era5Adapter(BaseIngestAdapter):
    """Hourly ERA5 single-level fields over the AOI cells, one NetCDF per request."""

    source: ClassVar[DataSource] = DataSource.era5_cds
    adapter_name: ClassVar[str] = "era5_cds"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = ERA5_LICENCE
    licence_source_url: ClassVar[str | None] = ERA5_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (CDS_CREDENTIAL,)

    def __init__(self, *, cds: CdsClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cds = cds

    @property
    def cds(self) -> CdsClient:
        if self._cds is None:
            key = self.settings.cdsapi_key
            if key is None:
                raise RuntimeError("CDS key missing; fetch() should have refused")
            self._cds = CdsapiClient(self.settings.cdsapi_url, key.get_secret_value())
        return self._cds

    @staticmethod
    def _variables(request: IngestRequest) -> tuple[str, ...]:
        raw = request.params.get("variables")
        if raw is None:
            return DEFAULT_VARIABLES
        return (raw,) if isinstance(raw, str) else tuple(str(v) for v in raw)

    @staticmethod
    def product_id(request: IngestRequest) -> str:
        assert request.time_start and request.time_end
        return (
            f"{ERA5_DATASET}_{request.aoi_id}_{request.time_start:%Y%m%d}_{request.time_end:%Y%m%d}"
        )

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """ERA5 is a continuous archive: the single 'product' is the request itself."""
        if request.time_start is None or request.time_end is None:
            raise ValueError("ERA5 needs time_start and time_end")
        variables = self._variables(request)
        cds_request = build_cds_request(
            request.bbox_4326, request.time_start, request.time_end, variables
        )
        area = era5_area(request.bbox_4326)
        rows, cols = era5_cells(area)
        n_days = len(date_list(request.time_start, request.time_end))
        n_values = rows * cols * len(HOURS) * n_days * len(variables)
        return [
            ProductRecord(
                source=self.source,
                product_id=self.product_id(request),
                product_level="reanalysis",
                url=f"{self.settings.cdsapi_url}/retrieve/v1/processes/{ERA5_DATASET}/execute",
                time_start=request.time_start,
                time_end=request.time_end,
                bbox_4326=(area[1], area[2], area[3], area[0]),
                estimated_bytes=n_values * BYTES_PER_VALUE,
                licence=self.licence,
                licence_source_url=self.licence_source_url,
                properties={
                    "dataset": ERA5_DATASET,
                    "cds_request": cds_request,
                    "grid_cells": [rows, cols],
                    "n_days": n_days,
                    "n_hours": len(HOURS),
                    "variables": list(variables),
                },
            )
        ]

    def plan(self, request: IngestRequest) -> DryRunPlan:
        products = self.search(request)
        p = products[0]
        rows, cols = p.properties["grid_cells"]
        basis = (
            f"{rows} x {cols} grid points at {ERA5_GRID_DEG} deg x {p.properties['n_hours']} "
            f"hourly steps x {p.properties['n_days']} day(s) x {len(p.properties['variables'])} "
            f"variable(s) x {BYTES_PER_VALUE} B float32, uncompressed NetCDF"
        )
        return self.build_plan(
            request,
            products,
            estimated_bytes=p.estimated_bytes,
            estimate_basis=basis,
            warnings=["CDS queues requests; retrieval time depends on the service load"],
        )

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        out = dest / OUTPUT_FILENAME
        self.cds.retrieve(ERA5_DATASET, dict(product.properties["cds_request"]), out)
        if not out.exists():
            raise FileNotFoundError(f"CDS client returned without writing {out}")
        sha, size = sha256_and_size(out)
        return [
            FetchedFile(
                path=out,
                sha256=sha,
                size_bytes=size,
                url=product.url,
                params={"dataset": ERA5_DATASET, "cds_request": product.properties["cds_request"]},
                notes="NetCDF as delivered by CDS (hourly, 0.25 deg, area grown to the grid)",
                product_level="reanalysis",
            )
        ]
