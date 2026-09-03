"""Provenance ledger records.

Every byte serac retrieves, requests, or declines to fetch is recorded as a `ManifestEntry`
in `data/manifest.jsonl`. This is the single source of truth for "what data do we actually
have, where did it come from, and under what licence". Nothing may be written under `data/`
without a matching entry, and nothing synthetic may ever be recorded as `provenance: real`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MANIFEST_CONTRACT_VERSION = "0.2.0"

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DataSource(StrEnum):
    """Where a record came from. One value per adapter (or per fixture provenance)."""

    sentinel1_asf = "sentinel1_asf"
    hyp3_insar = "hyp3_insar"
    sentinel2_cdse = "sentinel2_cdse"
    sentinel2_earthsearch = "sentinel2_earthsearch"
    nisar_asf = "nisar_asf"
    dem_glo30 = "dem_glo30"
    era5_cds = "era5_cds"
    gacos = "gacos"
    fdsn_waveforms = "fdsn_waveforms"
    seedlink = "seedlink"
    usgs_comcat = "usgs_comcat"
    hydrometric_icimod = "hydrometric_icimod"
    osm_overpass = "osm_overpass"
    source_document = "source_document"
    vendored_schema = "vendored_schema"
    esec_spud = "esec_spud"
    iris_syngine = "iris_syngine"
    rgi_glaciers = "rgi_glaciers"
    simulation_output = "simulation_output"
    serac_artefact = "serac_artefact"
    synthetic = "synthetic"


class ManifestStatus(StrEnum):
    """Lifecycle of a product with respect to this repository."""

    fetched = "fetched"  # bytes are on disk (or were, under DVC) and hashed
    listed = "listed"  # discovered via a search; nothing downloaded
    requested = "requested"  # an asynchronous job/request was submitted (HyP3, GACOS)
    not_fetched = "not_fetched"  # deliberately not retrieved (credentials, size, availability)
    failed = "failed"  # retrieval attempted and failed
    dry_run = "dry_run"  # plan only; never written to the ledger by adapters, kept for reports
    synthetic = "synthetic"  # a labelled synthetic placeholder under tests/fixtures/synthetic


class Retention(StrEnum):
    """Whether the recorded bytes are still on disk.

    `transient` records a file that was hashed on arrival and then deleted (a multi-GB HyP3
    zip cropped to an AOI, say). The sha256 is honest but no longer re-checkable, so
    `validate-ingest` reports these rows as a named warning rather than silently passing them.
    """

    retained = "retained"
    transient = "transient"


class Provenance(StrEnum):
    """Where a record's numbers come from.

    `real` is observed. `synthetic` is a fabricated stand-in for an observation and may only
    live under `tests/fixtures/synthetic/`. `derived` is computed: a reprojection, a feature
    cube, a simulation output, or physics evaluated from a published Earth model (Green's
    functions). The distinction matters because `derived` data is reproducible from stated
    inputs, whereas `synthetic` data stands in for something serac could not obtain.
    """

    real = "real"
    synthetic = "synthetic"
    derived = "derived"


class ManifestEntry(BaseModel):
    """One line of `data/manifest.jsonl`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = MANIFEST_CONTRACT_VERSION
    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    recorded_at: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    source: DataSource
    product_id: str = Field(min_length=1)
    product_level: str | None = None
    aoi_id: str | None = None
    event_id: str | None = None

    path: str | None = Field(default=None, description="Repo-relative path of the stored bytes")
    url: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    size_bytes: int | None = Field(default=None, ge=0)
    estimated_bytes: int | None = Field(default=None, ge=0)
    retrieved_at: AwareDatetime | None = None

    licence: str = Field(min_length=1, description="SPDX id or the licence text as stated")
    licence_source_url: str | None = None
    provenance: Provenance
    status: ManifestStatus

    time_start: AwareDatetime | None = None
    time_end: AwareDatetime | None = None
    bbox_4326: tuple[float, float, float, float] | None = None

    retention: Retention = Field(
        default=Retention.retained,
        description="`transient` means the bytes were hashed then deleted; see the class docs.",
    )
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    serac_git_sha: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.status == ManifestStatus.fetched:
            missing = [
                name
                for name in ("path", "sha256", "size_bytes", "retrieved_at")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"status=fetched requires {missing}")
        if self.status == ManifestStatus.synthetic and self.provenance != Provenance.synthetic:
            raise ValueError("status=synthetic requires provenance=synthetic")
        if self.provenance == Provenance.synthetic:
            if self.status not in (ManifestStatus.synthetic, ManifestStatus.dry_run):
                raise ValueError("provenance=synthetic entries must have status=synthetic")
            if self.path is not None and not self.path.startswith("tests/fixtures/synthetic/"):
                raise ValueError("synthetic data may only live under tests/fixtures/synthetic/")
            if not self.notes:
                raise ValueError("synthetic entries must carry notes explaining the placeholder")
        if (
            self.path is not None
            and self.path.startswith("data/")
            and (self.provenance == Provenance.synthetic)
        ):
            raise ValueError("nothing synthetic may be written under data/")
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("time_end must not precede time_start")
        if self.bbox_4326 is not None:
            w, s, e, n = self.bbox_4326
            if not (-180 <= w <= e <= 180 and -90 <= s <= n <= 90):
                raise ValueError("bbox_4326 must be (west, south, east, north) in degrees")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"manifest-entry": ManifestEntry}
