"""Ingestion port: `search` -> `plan` (dry run) -> `fetch`, with provenance on every byte.

Every adapter that brings external data into `data/` implements `IngestAdapter`. The contract
is deliberately three-step so that a `--dry-run` can print exactly what would be fetched, how
many bytes it would cost (and on what basis that estimate rests), which credentials it needs,
and what the adapter refuses to do, without touching the network for downloads or writing a
single ledger line. `fetch` is the only step that writes, and it always writes a
`ManifestEntry` for each file it produces, declines, or fails on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.manifest import DataSource, ManifestEntry
from serac.ports.ledger import ManifestLedger

INGEST_CONTRACT_VERSION = "0.1.0"

Bbox4326 = tuple[float, float, float, float]
"""(west, south, east, north) in EPSG:4326 degrees."""

ConfirmFn = Callable[[str], bool]
"""Callback the adapter uses to ask the operator before an expensive fetch; returns True to go."""


def _check_bbox(bbox: Bbox4326) -> None:
    w, s, e, n = bbox
    if not (-180 <= w <= e <= 180 and -90 <= s <= n <= 90):
        raise ValueError("bbox_4326 must be (west, south, east, north) in degrees")


class CredentialSpec(BaseModel):
    """A credential an adapter needs, described so the dry run can say what is missing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, description="Human name, e.g. 'Earthdata Login'")
    env_vars: tuple[str, ...] = Field(min_length=1, description="Settings fields (upper-case)")
    purpose: str = Field(min_length=1)
    docs: str = "docs/CREDENTIALS.md"


class IngestRequest(BaseModel):
    """What the caller wants: an AOI, a bbox, an optional time window, adapter-specific params."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aoi_id: str = Field(min_length=1)
    bbox_4326: Bbox4326
    time_start: AwareDatetime | None = None
    time_end: AwareDatetime | None = None
    event_id: str | None = None
    product_level: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        _check_bbox(self.bbox_4326)
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("time_end must not precede time_start")
        return self


class ProductRecord(BaseModel):
    """One product an adapter found (search) or intends to fetch (plan)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: DataSource
    product_id: str = Field(min_length=1)
    product_level: str | None = None
    url: str | None = None
    assets: dict[str, str] = Field(default_factory=dict, description="asset key -> href")
    time_start: AwareDatetime | None = None
    time_end: AwareDatetime | None = None
    bbox_4326: Bbox4326 | None = None
    estimated_bytes: int | None = Field(default=None, ge=0)
    licence: str = Field(min_length=1)
    licence_source_url: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.bbox_4326 is not None:
            _check_bbox(self.bbox_4326)
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("time_end must not precede time_start")
        return self


class DryRunPlan(BaseModel):
    """The output of `IngestAdapter.plan`: everything a `--dry-run` prints, nothing more."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = INGEST_CONTRACT_VERSION
    source: DataSource
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    request: IngestRequest
    products: list[ProductRecord] = Field(default_factory=list)
    estimated_bytes: int | None = Field(
        default=None, ge=0, description="null when the adapter cannot honestly estimate"
    )
    estimate_basis: str = Field(min_length=1, description="How estimated_bytes was derived")
    requires_credentials: list[CredentialSpec] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def fetchable(self) -> bool:
        """True when nothing in the plan forbids a fetch (credentials are checked at fetch)."""
        return not self.refusals and bool(self.products)


class IngestAdapter(ABC):
    """Port for anything that brings external products into `data/raw/<source>/<aoi>/`."""

    source: ClassVar[DataSource]
    adapter_name: ClassVar[str]
    adapter_version: ClassVar[str]

    @abstractmethod
    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """Discover products matching `request` without downloading any of them."""

    @abstractmethod
    def plan(self, request: IngestRequest) -> DryRunPlan:
        """Describe what `fetch` would do. Must not write anything, not even a ledger line."""

    @abstractmethod
    def fetch(
        self,
        plan: DryRunPlan,
        *,
        dest_root: Path,
        ledger: ManifestLedger,
        confirm: ConfirmFn,
    ) -> list[ManifestEntry]:
        """Execute `plan` under `dest_root/raw/<source>/<aoi>/<product>/`.

        Appends one `ManifestEntry` per produced file, and `not_fetched`/`failed` entries when
        it declines or fails. Calls `confirm` before any fetch whose estimate exceeds the size
        gate or cannot be estimated. Raises `CredentialsMissingError` after recording
        `not_fetched` entries when a required credential is absent.
        """
