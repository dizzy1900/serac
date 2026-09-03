"""Obviously fictional event-library material shared by the pipeline, CLI and suite tests.

Nothing here describes a real event: ids are `test-*`, urls live under `example.invalid`,
every number is a placeholder and the sha256s are repeated hex digits. The `fictional`
fixture hands tests a factory that writes such records, ledger rows and whole tmp repos.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.events_index import build_index
from serac.pipelines.sources import dump_record

FICTIONAL_TIME = datetime(2026, 1, 1, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

NULL_RANGE_PATHS = (
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

FIELD_NOTE = {
    "reason": "not_yet_researched",
    "public_estimates": [],
    "notes": "fictional test record: this figure has not been researched",
}


class Fictional:
    """Factory for fictional records, ledger rows and tmp repositories."""

    time = FICTIONAL_TIME
    sha_a = SHA_A
    sha_b = SHA_B
    sha_c = SHA_C

    def source(
        self,
        id: str = "test-src-1",
        kind: str = "peer_reviewed",
        claims: list[str] | None = None,
        sha256: str = SHA_A,
        **overrides: Any,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": id,
            "kind": kind,
            "title": "Fictional test source",
            "url": f"https://example.invalid/{id}",
            "doi": None,
            "authors": [],
            "year": None,
            "publisher": None,
            "accessed_utc": FICTIONAL_TIME.isoformat(),
            "sha256": sha256,
            "content_type": "text/html",
            "licence": "CC-BY-4.0",
            "stored_copy": None,
            "claims_supported": claims or ["fall_height_m", "time", "source_location", "seismic"],
            "excerpt": None,
            "peer_reviewed": kind == "peer_reviewed",
        }
        data.update(overrides)
        return data

    def event(
        self,
        event_id: str = "test-event-1",
        *,
        role: str = "reference",
        failure_type: str = "bedrock_rock_ice_avalanche",
        time: datetime = FICTIONAL_TIME,
        aoi_id: str | None = None,
        best: float | None = 1.5,
        sha256: str = SHA_A,
        source_kind: str = "peer_reviewed",
        **overrides: Any,
    ) -> dict[str, Any]:
        """A valid minimal fictional record: one source, `fall_height_m` populated."""
        data: dict[str, Any] = {
            "event_id": event_id,
            "name": f"Fictional event {event_id}",
            "event_group": event_id,
            "role": role,
            "aoi_id": aoi_id,
            "failure_type": failure_type,
            "time": {
                "datetime_utc": time.isoformat(),
                "basis": "test",
                "source_refs": ["test-src-1"],
            },
            "source_location": {
                "lat": 1.0,
                "lon": 2.0,
                "basis": "test",
                "source_refs": ["test-src-1"],
            },
            "fall_height_m": {
                "low": 1.0,
                "high": 2.0,
                "best": best,
                "unit": "m",
                "source_refs": ["test-src-1"],
            },
            "seismic": {"usgs_id": "testid1", "source_refs": ["test-src-1"]},
            "dammed_river": False,
            "secondary_surge": False,
            "field_notes": {path: copy.deepcopy(FIELD_NOTE) for path in NULL_RANGE_PATHS},
            "sources": [self.source(kind=source_kind, sha256=sha256)],
            "record": {"created_utc": time.isoformat(), "created_by": "test"},
        }
        data.update(overrides)
        return data

    def library(self) -> list[dict[str, Any]]:
        """The four records `validate-events` needs: target, negative control, counterfactual,
        co-seismic reference. All fictional."""
        target = self.event("test-target", role="target")
        negative = self.event(
            "test-negative", role="negative_control", failure_type="moraine_collapse_glof"
        )
        co_seismic = self.event(
            "test-co-seismic", role="co_seismic_reference", failure_type="co_seismic_avalanche"
        )
        counterfactual = self.event(
            "test-counterfactual",
            role="evacuation_counterfactual",
            precursors_observed=[
                {
                    "kind": "displacement_acceleration",
                    "lead_time_days": {
                        "low": 1.0,
                        "high": 2.0,
                        "unit": "d",
                        "source_refs": ["test-src-1"],
                    },
                    "description": "fictional monitoring anomaly",
                    "source_refs": ["test-src-1"],
                }
            ],
            infrastructure_impacts=[
                {
                    "asset_name": "fictional village",
                    "asset_type": "settlement",
                    "impact": "evacuated",
                    "source_refs": ["test-src-1"],
                }
            ],
        )
        counterfactual["sources"][0]["claims_supported"].append(
            "precursors_observed[0].lead_time_days"
        )
        return [target, negative, co_seismic, counterfactual]

    def ledger_row(
        self,
        source: DataSource = DataSource.source_document,
        status: ManifestStatus = ManifestStatus.listed,
        *,
        sha256: str | None = SHA_A,
        event_id: str | None = "test-event-1",
        aoi_id: str | None = None,
        product_level: str | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        **overrides: Any,
    ) -> ManifestEntry:
        data: dict[str, Any] = {
            "source": source,
            "product_id": f"fictional/{source.value}/{event_id or aoi_id}",
            "product_level": product_level,
            "event_id": event_id,
            "aoi_id": aoi_id,
            "url": "https://example.invalid/ledger",
            "sha256": sha256,
            "licence": "CC-BY-4.0",
            "provenance": Provenance.real,
            "status": status,
            "time_start": time_start,
            "time_end": time_end,
            "adapter": "test",
            "adapter_version": "0",
        }
        if status == ManifestStatus.fetched:
            data.update(
                {
                    "path": "data/fixtures/fictional",
                    "size_bytes": 1,
                    "retrieved_at": FICTIONAL_TIME,
                }
            )
        data.update(overrides)
        return ManifestEntry(**data)

    def write(self, events_dir: Path, *records: dict[str, Any]) -> list[Path]:
        events_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for record in records:
            path = events_dir / f"{record['event_id']}.json"
            path.write_text(dump_record(record), encoding="utf-8")
            paths.append(path)
        return paths

    def repo(
        self,
        root: Path,
        records: list[dict[str, Any]] | None = None,
        *,
        ledger_rows: list[ManifestEntry] | None = None,
        index: bool = True,
    ) -> Path:
        """A tmp repository: records, a ledger row for their sha256, and (optionally) the index."""
        chosen = self.library() if records is None else records
        self.write(root / "data" / "events", *chosen)
        ledger = JsonlManifestLedger(root / "data" / "manifest.jsonl")
        rows = [self.ledger_row()] if ledger_rows is None else ledger_rows
        for row in rows:
            ledger.append(row)
        if index:
            build_index(root / "data" / "events")
        return root

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded


@pytest.fixture
def fictional() -> Fictional:
    return Fictional()
