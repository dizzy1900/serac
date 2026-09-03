"""Replay of the real fixtures at speed max on the in-memory bus (offline)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from serac.domain.replay import ReplayReport
from serac.pipelines.replay import ReplayConfig, load_replay_report, run_replay
from serac.streaming.replay_source import FixtureNotFetchedError


@pytest.mark.parametrize("event_id", ["chamoli-2021", "langtang-2026"])
def test_fixture_replay_completes_and_writes_a_valid_report(
    repo_root: Path, tmp_path: Path, event_id: str
) -> None:
    config = ReplayConfig(
        event_id=event_id, speed="max", bus="in_memory", report_dir=tmp_path, repo_root=repo_root
    )
    started = time.monotonic()
    report = run_replay(config)
    assert time.monotonic() - started < 10.0

    assert report.status == "completed" and report.error is None
    assert report.event_id == event_id
    assert report.contains_synthetic is False
    assert all(f.provenance == "real" for f in report.fixtures)
    assert {Path(f.path).name for f in report.fixtures} >= {"stations.xml"}
    counts = report.counts
    assert counts.chunks_published > 0
    assert counts.chunks_consumed == counts.chunks_published
    assert counts.pending_after_drain == 0
    assert counts.cap_messages_emitted == counts.detections_emitted
    assert report.is_stub is True and report.detector.is_stub is True
    assert report.detector.params["threshold_is_placeholder"] is True
    assert report.wall_clock_latencies.valid is False
    assert report.chunk_seconds == 5.0
    assert any("STUB" in c for c in report.caveats)
    assert any("plumbing" in c for c in report.caveats)

    # Whether the stub fired is recorded, never asserted; the first-detection mark is
    # consistent with the counts either way.
    assert (report.first_detection is None) == (counts.detections_emitted == 0)
    assert (report.first_cap is None) == (counts.cap_messages_emitted == 0)

    # Origin: read from the event record if present, otherwise null with a caveat.
    if report.origin_time_utc is None:
        assert report.stream_time_latencies.origin_to_first_detection_s is None
        assert report.stream_time_latencies.origin_to_first_cap_s is None
        assert any("origin-relative latencies are null" in c for c in report.caveats)
    else:
        assert report.origin_time_source is not None

    stations = {s.sncl: s for s in report.stations}
    assert "NK.KKN..BHZ" in stations
    assert stations["NK.KKN..BHZ"].latitude == 27.8
    assert sum(s.chunks_published for s in report.stations) == counts.chunks_published

    path = tmp_path / f"{event_id}.json"
    assert path.exists()
    assert load_replay_report(path) == report
    schema = json.loads((repo_root / "contracts" / "replay-report.v0.json").read_text())
    Draft202012Validator(schema).validate(json.loads(path.read_text()))


def test_origin_time_comes_from_a_fictional_event_record(repo_root: Path, tmp_path: Path) -> None:
    """A fictional record in tmp (no real numbers) drives origin time and station distances."""
    fixture_src = repo_root / "data" / "fixtures" / "seismic" / "chamoli-2021"
    root = tmp_path / "repo"
    fixture_dst = root / "data" / "fixtures" / "seismic" / "test-event"
    fixture_dst.mkdir(parents=True)
    for file in fixture_src.iterdir():
        fixture_dst.joinpath(file.name).write_bytes(file.read_bytes())
    manifest = json.loads((fixture_dst / "manifest.json").read_text())
    manifest["event_id"] = "test-event"
    (fixture_dst / "manifest.json").write_text(json.dumps(manifest))
    (root / "contracts" / "vendor" / "cap").mkdir(parents=True)
    for name in ("CAP-v1.2.xsd",):
        (root / "contracts" / "vendor" / "cap" / name).write_bytes(
            (repo_root / "contracts" / "vendor" / "cap" / name).read_bytes()
        )
    record = _fictional_record()
    events = root / "data" / "events"
    events.mkdir(parents=True)
    (events / "test-event.json").write_text(json.dumps(record))

    report = run_replay(
        ReplayConfig(event_id="test-event", speed="max", repo_root=root, report_dir=tmp_path / "r")
    )
    assert report.status == "completed"
    assert report.origin_time_source == "test-event"
    assert report.origin_time_utc is not None
    assert report.origin_time_utc.isoformat() == "2021-02-07T04:50:00+00:00"
    kkn = next(s for s in report.stations if s.sncl == "NK.KKN..BHZ")
    assert kkn.distance_from_source_km is not None and 0 < kkn.distance_from_source_km < 20
    lat = report.stream_time_latencies
    if report.first_detection is not None:
        assert lat.origin_to_first_detection_s is not None
        assert lat.origin_to_first_detection_s == pytest.approx(
            (report.first_detection.stream_time_utc - report.origin_time_utc).total_seconds()
        )
    assert not any("origin-relative latencies are null" in c for c in report.caveats)


def _fictional_record() -> dict[str, object]:
    """Minimal `MassMovementEvent` JSON; every value is a placeholder near NK.KKN, not an event."""
    src = {
        "id": "test-src",
        "kind": "dataset",
        "title": "Fictional test source",
        "url": "https://example.invalid/test-src",
        "accessed_utc": "2026-01-01T00:00:00Z",
        "sha256": "a" * 64,
        "content_type": "text/html",
        "licence": "CC0-1.0",
        "claims_supported": ["time", "source_location"],
        "peer_reviewed": False,
    }
    return {
        "event_id": "test-event",
        "name": "Fictional test event",
        "event_group": "test-event",
        "role": "reference",
        "failure_type": "bedrock_rock_ice_avalanche",
        "time": {
            "datetime_utc": "2021-02-07T04:50:00Z",
            "basis": "fictional",
            "source_refs": ["test-src"],
        },
        "source_location": {
            "lat": 27.85,
            "lon": 85.20,
            "basis": "fictional",
            "source_refs": ["test-src"],
        },
        "dammed_river": False,
        "secondary_surge": False,
        "field_notes": {
            name: {"reason": "not_applicable", "notes": "fictional test record; no figure exists"}
            for name in (
                "source_elevation_m",
                "fall_height_m",
                "source_volume_m3",
                "rock_fraction",
                "bulked_volume_m3",
                "runout_km",
                "peak_velocity_ms",
                "fatalities",
                "seismic",
            )
        },
        "sources": [src],
        "record": {"created_utc": "2026-01-01T00:00:00Z", "created_by": "test"},
    }


def test_missing_fixture_without_online_is_refused(repo_root: Path, tmp_path: Path) -> None:
    with pytest.raises(FixtureNotFetchedError, match="--online"):
        run_replay(ReplayConfig(event_id="no-such-event", repo_root=repo_root, report_dir=tmp_path))


def test_report_model_round_trips(repo_root: Path, tmp_path: Path) -> None:
    report = run_replay(
        ReplayConfig(event_id="chamoli-2021", repo_root=repo_root, report_dir=tmp_path)
    )
    assert ReplayReport.model_validate_json(report.model_dump_json()) == report
