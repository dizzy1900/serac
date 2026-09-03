"""Hydrometric source backed by a fixture of ICIMOD-reported figures (`HydrometricSource`).

There is no open real-time hydrometric feed for the Nepal/China corridor (RELEASE_STATUS.md
Known gaps 2). What exists is public reporting: `data/fixtures/hydro/<name>.json` transcribes
figures from a retrieved ICIMOD page, and every observation carries the `source_ref` and the
verbatim sentence it was read from. Anything the fixture does not state is not available:
`observations()` raises `DatasetNotFetchedError` rather than returning an empty series that
could be mistaken for "no change", and a fixture whose `status` is `not_fetched` raises on
every call.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import DatasetNotFetchedError, SeracError
from serac.ports.seismic import HydrometricSource, HydroObservation, HydroStation

ADAPTER_NAME = "IcimodReportedHydrometric"
ADAPTER_VERSION = "0.1.0"
DEFAULT_FIXTURE = Path("data") / "fixtures" / "hydro" / "icimod_trishuli_2026-08-26.json"


class HydroFixtureError(SeracError):
    """The fixture file is malformed."""


class HydroSourceDocument(BaseModel):
    """The retrieved document the figures were transcribed from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    published_at: AwareDatetime | None = None
    accessed_utc: AwareDatetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content_type: str | None = None
    licence: str = Field(min_length=1)
    licence_source_url: str | None = None
    stored_copy: str | None = None
    notes: str | None = None


class HydroFixture(BaseModel):
    """`data/fixtures/hydro/*.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "0.1.0"
    status: Literal["fetched", "not_fetched"]
    event_id: str | None = None
    event_date: date | None = None
    description: str | None = None
    reason: str | None = Field(default=None, description="Why nothing was fetched.")
    sources: list[HydroSourceDocument] = Field(default_factory=list)
    stations: list[HydroStation] = Field(default_factory=list)
    observations: list[HydroObservation] = Field(default_factory=list)
    excerpts: list[str] = Field(default_factory=list)

    def check(self) -> None:
        """Cross-field rules: fetched fixtures cite a source for every station and value."""
        if self.status == "not_fetched":
            if self.stations or self.observations or self.sources:
                raise HydroFixtureError("a not_fetched fixture must not carry data")
            if not self.reason:
                raise HydroFixtureError("a not_fetched fixture must state a reason")
            return
        if not self.sources:
            raise HydroFixtureError("a fetched fixture needs at least one source document")
        if self.event_date is None:
            raise HydroFixtureError("a fetched fixture needs event_date")
        ids = {s.id for s in self.sources}
        station_ids = {s.station_id for s in self.stations}
        for station in self.stations:
            missing = set(station.source_refs) - ids
            if missing or not station.source_refs:
                raise HydroFixtureError(f"station {station.station_id}: unresolved source_refs")
        for obs in self.observations:
            if obs.source_ref not in ids:
                raise HydroFixtureError(f"observation for {obs.station_id}: unknown source_ref")
            if obs.station_id not in station_ids:
                raise HydroFixtureError(f"observation for unknown station {obs.station_id}")
            if obs.time_utc is None and not obs.excerpt:
                raise HydroFixtureError(
                    f"untimed observation for {obs.station_id} must quote its source sentence"
                )


def load_fixture(path: Path) -> HydroFixture:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetNotFetchedError(f"hydrometric fixture missing: {path}") from exc
    fixture = HydroFixture.model_validate(doc)
    fixture.check()
    return fixture


class IcimodReportedHydrometric(HydrometricSource):
    """Reported (not gauged) Trishuli figures; every number cites the page it came from."""

    def __init__(self, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path if fixture_path is not None else DEFAULT_FIXTURE
        self._fixture: HydroFixture | None = None

    @property
    def fixture(self) -> HydroFixture:
        if self._fixture is None:
            self._fixture = load_fixture(self.fixture_path)
        return self._fixture

    def _require_fetched(self) -> HydroFixture:
        fixture = self.fixture
        if fixture.status != "fetched":
            raise DatasetNotFetchedError(
                f"{self.fixture_path}: status={fixture.status}; {fixture.reason or 'no data'}"
            )
        return fixture

    def stations(self) -> list[HydroStation]:
        return list(self._require_fetched().stations)

    def _event_day(self) -> tuple[datetime, datetime]:
        day = self._require_fetched().event_date
        if day is None:  # pragma: no cover - check() guarantees it for fetched fixtures
            raise HydroFixtureError("fetched fixture lacks event_date")
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return start, datetime.combine(day, time.max, tzinfo=UTC)

    def observations(
        self, station_id: str, window: tuple[datetime, datetime]
    ) -> list[HydroObservation]:
        """Observations for `station_id` in `window`.

        Timed observations match by `time_utc`. Untimed ones (the source gave no clock time)
        are attributed to the fixture's `event_date` and match any window overlapping that
        day; their `time_basis` says the time was not stated.
        """
        fixture = self._require_fetched()
        if station_id not in {s.station_id for s in fixture.stations}:
            raise DatasetNotFetchedError(f"no station {station_id!r} in {self.fixture_path}")
        start, end = window
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
        day_start, day_end = self._event_day()
        out: list[HydroObservation] = []
        for obs in fixture.observations:
            if obs.station_id != station_id:
                continue
            if obs.time_utc is not None:
                if start <= obs.time_utc <= end:
                    out.append(obs)
            elif start <= day_end and end >= day_start:
                out.append(obs)
        if not out:
            raise DatasetNotFetchedError(
                f"no reported observations for {station_id!r} in the window; the fixture holds "
                "only the figures ICIMOD published, not a gauge series"
            )
        return out
