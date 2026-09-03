"""Factories for obviously fictional domain records shared by the domain tests.

Nothing here describes a real event: ids are `test-*`, urls live under `example.invalid`,
and every number is a placeholder.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from serac.domain.common import (
    FieldNote,
    FieldNoteReason,
    Range,
    RecordMeta,
    SourceKind,
    SourceRef,
)
from serac.domain.events import (
    EventRole,
    EventTime,
    FailureType,
    MassMovementEvent,
    SeismicAttribution,
    SourceLocation,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64

SourceFactory = Callable[..., SourceRef]
RangeFactory = Callable[..., Range]


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def make_source() -> SourceFactory:
    def _make(
        id: str = "test-src-1",
        kind: SourceKind = SourceKind.peer_reviewed,
        claims: list[str] | None = None,
        **overrides: Any,
    ) -> SourceRef:
        data: dict[str, Any] = {
            "id": id,
            "kind": kind,
            "title": "Fictional test source",
            "url": f"https://example.invalid/{id}",
            "accessed_utc": NOW,
            "sha256": SHA_A,
            "content_type": "text/html",
            "licence": "CC-BY-4.0",
            "claims_supported": claims or ["fall_height_m"],
            "peer_reviewed": kind == SourceKind.peer_reviewed,
        }
        data.update(overrides)
        return SourceRef(**data)

    return _make


@pytest.fixture
def make_range() -> RangeFactory:
    def _make(
        low: float = 1.0,
        high: float = 2.0,
        best: float | None = None,
        unit: str = "m",
        source_refs: list[str] | None = None,
        **overrides: Any,
    ) -> Range:
        data: dict[str, Any] = {
            "low": low,
            "high": high,
            "best": best,
            "unit": unit,
            "source_refs": source_refs or ["test-src-1"],
        }
        data.update(overrides)
        return Range(**data)

    return _make


@pytest.fixture
def field_note() -> FieldNote:
    return FieldNote(
        reason=FieldNoteReason.not_yet_researched,
        notes="fictional test record: this figure has not been researched",
    )


ALL_NULL_RANGE_PATHS = (
    "source_elevation_m",
    "source_volume_m3",
    "rock_fraction",
    "bulked_volume_m3",
    "runout_km",
    "peak_velocity_ms",
    "fatalities",
    "seismic.magnitude",
    "seismic.agency_range",
)


@pytest.fixture
def event_kwargs(
    make_source: SourceFactory, make_range: RangeFactory, field_note: FieldNote
) -> dict[str, Any]:
    """A valid minimal fictional event: one peer-reviewed source, `fall_height_m` populated."""
    return {
        "event_id": "test-event-1",
        "name": "Fictional test event",
        "event_group": "test-event",
        "role": EventRole.reference,
        "failure_type": FailureType.bedrock_rock_ice_avalanche,
        "time": EventTime(datetime_utc=NOW, basis="test", source_refs=["test-src-1"]),
        "source_location": SourceLocation(
            lat=1.0, lon=2.0, basis="test", source_refs=["test-src-1"]
        ),
        "fall_height_m": make_range(best=1.5),
        "seismic": SeismicAttribution(usgs_id="testid1", source_refs=["test-src-1"]),
        "dammed_river": False,
        "secondary_surge": False,
        "field_notes": {path: field_note for path in ALL_NULL_RANGE_PATHS},
        "sources": [make_source(claims=["fall_height_m", "time", "source_location", "seismic"])],
        "record": RecordMeta(created_utc=NOW, created_by="test"),
    }


@pytest.fixture
def make_event(event_kwargs: dict[str, Any]) -> Callable[..., MassMovementEvent]:
    def _make(**overrides: Any) -> MassMovementEvent:
        data = dict(event_kwargs)
        data.update(overrides)
        return MassMovementEvent(**data)

    return _make
