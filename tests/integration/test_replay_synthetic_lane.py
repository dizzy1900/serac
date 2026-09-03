"""The synthetic lane proves detection -> CAP end to end and is labelled synthetic throughout."""

from __future__ import annotations

from pathlib import Path

from serac.adapters.bus.in_memory import InMemoryBus
from serac.domain import topics
from serac.domain.cap import CAPMessage
from serac.domain.detection import DetectionCandidate
from serac.domain.seismic import SeismicTrace
from serac.pipelines.replay import ReplayConfig, run_replay
from serac.streaming.replay_source import SYNTHETIC_EVENT_ID
from serac.validation.cap import CapValidator


def test_synthetic_lane_yields_detection_and_valid_cap(repo_root: Path, tmp_path: Path) -> None:
    bus = InMemoryBus()
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID, speed="max", repo_root=repo_root, report_dir=tmp_path
    )
    report = run_replay(config, bus=bus)
    assert report.status == "completed"
    assert report.contains_synthetic is True
    assert report.fixtures[0].provenance == "synthetic"
    assert report.fixtures[0].path.startswith("synthetic://")
    assert not any(f.path.startswith("data/") for f in report.fixtures)
    assert report.origin_time_utc is not None  # burst onset, known by construction
    assert report.origin_time_source is not None and "synthetic" in report.origin_time_source
    assert report.counts.detections_emitted >= 1
    assert report.counts.cap_messages_emitted >= 1
    assert report.counts.pending_after_drain == 0
    assert report.detector.params["allow_synthetic"] is True
    assert any("SYNTHETIC" in c for c in report.caveats)
    lat = report.stream_time_latencies
    assert lat.origin_to_first_detection_s is not None and lat.origin_to_first_detection_s > 0
    assert lat.first_detection_to_first_cap_s == 0.0  # CAP inherits the detection's stream time

    waveforms = bus.log(topics.WAVEFORMS)
    assert all(isinstance(e.payload, SeismicTrace) and e.payload.is_synthetic for e in waveforms)
    assert all(e.replay_run_id == report.replay_run_id for e in waveforms)
    detections = bus.log(topics.DETECTIONS)
    assert detections and all(isinstance(e.payload, DetectionCandidate) for e in detections)
    alerts = bus.log(topics.ALERTS)
    validator = CapValidator(repo_root / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd")
    assert len(alerts) == report.counts.cap_messages_emitted
    for envelope in alerts:
        message = envelope.payload
        assert isinstance(message, CAPMessage)
        assert message.status == "Test" and message.scope == "Private"
        assert message.sender == "serac-stub@serac.invalid"
        assert all(info.area == [] for info in message.info)
        assert message.xml is not None and validator.errors(message.xml) == []
        assert envelope.causation_id in {d.message_id for d in detections}

    assert (tmp_path / f"{SYNTHETIC_EVENT_ID}.json").exists()
    assert not (repo_root / "data" / "fixtures" / "seismic" / SYNTHETIC_EVENT_ID).exists()
