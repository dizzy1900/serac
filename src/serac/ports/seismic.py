"""Ports for seismic and hydrometric data sources.

Adapters (Prompt 1 phase 2): `FdsnWaveformArchive` (ObsPy fdsnws client), `SeedLinkFeed`
(ObsPy EasySeedLinkClient), `ComCatCatalog` (USGS event service, `eventtype=landslide`),
`IcimodReportedHydrometric` (fixture of cited figures). This module holds only the ABCs and
their request/result value objects; nothing here imports obspy or numpy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.domain.manifest import ManifestEntry
from serac.domain.seismic import SeismicTrace, Sncl
from serac.ports.ledger import ManifestLedger


class StationQuery(BaseModel):
    """Station search by radius around a point, optionally restricted by network/channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    max_radius_km: float = Field(gt=0)
    min_radius_km: float = Field(default=0, ge=0)
    networks: list[str] = Field(default_factory=list, description="Empty means any network.")
    channels: list[str] = Field(
        default_factory=lambda: ["BHZ", "HHZ"], description="Channel patterns (fdsnws syntax)."
    )
    start_utc: AwareDatetime
    end_utc: AwareDatetime
    include_restricted: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc")
        if self.min_radius_km >= self.max_radius_km:
            raise ValueError("min_radius_km must be smaller than max_radius_km")
        return self


class StationRef(BaseModel):
    """A channel that exists at a data centre for the queried window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sncl: Sncl
    latitude: float
    longitude: float
    elevation_m: float | None = None
    sampling_rate_hz: float | None = Field(default=None, gt=0)
    distance_km: float | None = Field(default=None, ge=0)
    data_centre: str = Field(min_length=1, description="Resolved base URL, never an alias.")
    restricted: bool | None = None


class WaveformRequest(BaseModel):
    """What to pull from an archive for one event window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    sncls: list[Sncl] = Field(min_length=1)
    start_utc: AwareDatetime
    end_utc: AwareDatetime
    with_stations: bool = Field(default=True, description="Also fetch StationXML (channel).")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc")
        return self


class FetchPlan(BaseModel):
    """Dry-run description of a fetch: what would be requested and the byte estimate basis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: WaveformRequest
    data_centre: str = Field(min_length=1)
    bulk: list[list[str]] = Field(description="fdsnws bulk rows: net sta loc cha start end.")
    estimated_bytes: int = Field(ge=0)
    estimate_basis: str = Field(
        min_length=1, description="Stated assumption, e.g. `~1.2 bytes/sample Steim2 at 50 Hz`."
    )
    warnings: list[str] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)


class FetchResult(BaseModel):
    """Outcome of a fetch: files written and ledger entries appended."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: FetchPlan
    dest_dir: str
    files: list[str] = Field(default_factory=list, description="Repo-relative paths written.")
    missing: list[str] = Field(default_factory=list, description="SNCLs with no data.")
    entries: list[ManifestEntry] = Field(default_factory=list)
    status: Literal["fetched", "partial", "not_fetched"]


class WaveformArchive(ABC):
    """Archived waveform access (fdsnws-dataselect / fdsnws-station)."""

    @abstractmethod
    def search_stations(self, query: StationQuery) -> list[StationRef]:
        """Channels within the radius that have metadata for the window."""

    @abstractmethod
    def plan(self, request: WaveformRequest) -> FetchPlan:
        """Describe the fetch without touching the network beyond metadata."""

    @abstractmethod
    def fetch(self, plan: FetchPlan, dest_dir: Path, ledger: ManifestLedger) -> FetchResult:
        """Execute a plan: write MiniSEED + StationXML + sidecar manifest, append ledger rows."""


class WaveformFeed(ABC):
    """Live waveform access (SeedLink)."""

    @abstractmethod
    def subscribe(self, sncls: Sequence[Sncl]) -> None:
        """Select channels before `run`."""

    @abstractmethod
    def run(self, on_chunk: Callable[[SeismicTrace], None], *, max_chunks: int | None) -> int:
        """Deliver chunks to `on_chunk` until `max_chunks` or disconnect; return chunk count."""

    @abstractmethod
    def close(self) -> None:
        """Disconnect."""


class CatalogQuery(BaseModel):
    """Event-catalogue search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_utc: AwareDatetime
    end_utc: AwareDatetime
    event_type: str | None = Field(default="landslide", description="e.g. `landslide`.")
    min_magnitude: float | None = None
    bbox_4326: tuple[float, float, float, float] | None = None
    event_id: str | None = Field(default=None, description="Exact id lookup, e.g. us7000tbwb.")
    limit: int = Field(default=20000, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc")
        return self


class CatalogEvent(BaseModel):
    """One catalogue event (a ComCat feature, flattened)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    time_utc: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    depth_km: float | None = None
    magnitude: float | None = None
    mag_type: str | None = None
    event_type: str | None = None
    title: str | None = None
    url: str | None = None
    source_agency: str | None = None
    raw: dict[str, object] = Field(default_factory=dict, description="Feature `properties`.")


class EventCatalog(ABC):
    """Event-catalogue access (USGS ComCat)."""

    @abstractmethod
    def query(self, query: CatalogQuery) -> list[CatalogEvent]:
        """Events matching the query, paginated internally."""


class HydroStation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str = Field(min_length=1)
    name: str
    river: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    operator: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class HydroObservation(BaseModel):
    """One reported reading; every value must carry its own citation.

    `time_utc` is null when the source states the reading without a time (a press statement
    such as "rose nine metres within 30 minutes" carries an interval but no clock time); the
    adapter then attributes it to the report's `event_date` and `time_basis` says so. A
    `stage_change_m` value without `interval_s` is meaningless, so the pair is enforced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str = Field(min_length=1)
    time_utc: AwareDatetime | None = None
    time_basis: str = Field(
        min_length=1, description="e.g. `gauge_timestamp`, `not_stated_in_source`."
    )
    variable: Literal["stage_m", "discharge_m3s", "stage_change_m"]
    value: float
    interval_s: float | None = Field(
        default=None, gt=0, description="Interval over which a `stage_change_m` accrued."
    )
    uncertainty: float | None = Field(default=None, ge=0)
    source_ref: str = Field(min_length=1)
    excerpt: str | None = Field(default=None, description="Verbatim sentence the value comes from.")
    notes: str | None = None

    @model_validator(mode="after")
    def _change_needs_interval(self) -> Self:
        if self.variable == "stage_change_m" and self.interval_s is None:
            raise ValueError("stage_change_m requires interval_s")
        return self


class HydrometricSource(ABC):
    """Hydrometric access. No open real-time Nepal/China feed exists; see RELEASE_STATUS.md."""

    @abstractmethod
    def stations(self) -> list[HydroStation]:
        """Known stations."""

    @abstractmethod
    def observations(
        self, station_id: str, window: tuple[datetime, datetime]
    ) -> list[HydroObservation]:
        """Observations in the window; raises `DatasetNotFetchedError` when none are recorded."""
