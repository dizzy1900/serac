"""Detector stub behaviour on synthetic input; nothing here asserts real-event performance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from pydantic import BaseModel

from serac.adapters.bus.in_memory import InMemoryBus
from serac.domain import topics
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace
from serac.streaming import detector_stub
from serac.streaming.detector_stub import (
    STUB_MARKER,
    DetectorStub,
    DetectorStubConfig,
    SyntheticInputRefusedError,
    lp_sp_ratio,
)
from serac.streaming.stage import StageRunner
from serac.streaming.synthetic import synthetic_chunks, synthetic_lp_burst

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_module_docstring_starts_with_the_stub_marker() -> None:
    doc = detector_stub.__doc__
    assert doc is not None and doc.splitlines()[0] == STUB_MARKER


def test_default_config_declares_the_threshold_a_placeholder() -> None:
    params = DetectorStubConfig().as_params()
    assert params["threshold"] == 10.0
    assert params["threshold_is_placeholder"] is True
    assert params["buffer_seconds"] == 120.0
    assert params["lp_band_hz"] == "0.02-0.1" and params["sp_band_hz"] == "1.0-10.0"


def test_ratio_separates_a_long_period_tone_from_a_short_period_tone() -> None:
    fs = 20.0
    t = np.arange(0, 120, 1 / fs)
    lp = np.sin(2 * np.pi * t / 20.0)  # 0.05 Hz
    sp = np.sin(2 * np.pi * 3.0 * t)  # 3 Hz
    band = {"lp_band": (0.02, 0.1), "sp_band": (1.0, 10.0)}
    _, _, r_lp = lp_sp_ratio(lp, fs, **band)
    _, _, r_sp = lp_sp_ratio(sp, fs, **band)
    assert r_lp > 100
    assert r_sp < 0.01


def test_refuses_synthetic_chunks_unless_allowed() -> None:
    chunk = next(synthetic_chunks(start_utc=T0, n_chunks=1))
    with pytest.raises(SyntheticInputRefusedError):
        DetectorStub().evaluate(chunk)
    assert DetectorStub(DetectorStubConfig(allow_synthetic=True)).evaluate(chunk) is None


def test_no_ratio_before_min_fill_then_one_per_chunk() -> None:
    det = DetectorStub(DetectorStubConfig(allow_synthetic=True, min_fill_seconds=30))
    for chunk in synthetic_chunks(start_utc=T0, n_chunks=10, chunk_seconds=5):
        det.evaluate(chunk)
    assert det.chunks_seen == 10
    # 30 s fill needs 6 chunks; ratios for chunks 6..10.
    assert len(det.history) == 5
    assert all(h.n_samples <= 120 * 20 for h in det.history)


def test_lp_burst_fires_once_then_cooldown() -> None:
    det = DetectorStub(DetectorStubConfig(allow_synthetic=True, cooldown_seconds=1000))
    candidates: list[DetectionCandidate] = []
    for chunk in synthetic_lp_burst(start_utc=T0):
        found = det.evaluate(chunk)
        if found is not None:
            candidates.append(found)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.is_stub is True
    assert cand.source_location is None
    assert cand.score > cand.threshold == 10.0
    assert cand.detector == "lp-sp-ratio-stub"
    assert cand.window_start_utc <= cand.detected_at_stream_utc <= cand.window_end_utc
    assert cand.input_trace_ids
    assert cand.notes and STUB_MARKER in cand.notes
    assert sum(1 for h in det.history if h.fired) == 1
    assert any(h.ratio > 10 and not h.fired for h in det.history)  # suppressed by cooldown


def test_gap_resets_the_buffer() -> None:
    det = DetectorStub(DetectorStubConfig(allow_synthetic=True, min_fill_seconds=20))
    first = list(synthetic_chunks(start_utc=T0, n_chunks=6, chunk_seconds=5))
    later = list(
        synthetic_chunks(start_utc=T0 + timedelta(minutes=10), n_chunks=3, chunk_seconds=5)
    )
    for chunk in first + later:
        det.evaluate(chunk)
    assert det.gap_resets == 1
    # After the reset only 15 s are buffered: below min fill, so no new ratio.
    assert len(det.history) == 3  # from chunks 4,5,6 of the first run (20, 25, 30 s)


def test_buffer_is_bounded() -> None:
    det = DetectorStub(
        DetectorStubConfig(allow_synthetic=True, buffer_seconds=20, min_fill_seconds=10)
    )
    for chunk in synthetic_chunks(start_utc=T0, n_chunks=12, chunk_seconds=5):
        det.evaluate(chunk)
    assert max(h.n_samples for h in det.history) == 20 * 20
    last = det.history[-1]
    assert last.window_end_utc - last.window_start_utc == timedelta(seconds=(400 - 1) / 20)


def test_stage_publishes_detections_with_causation(tmp_path: object) -> None:
    bus = InMemoryBus()
    det = DetectorStub(DetectorStubConfig(allow_synthetic=True))
    runner = StageRunner(bus, det)
    for chunk in synthetic_lp_burst(start_utc=T0):
        env: Envelope[BaseModel] = wrap(
            chunk,
            topic=topics.WAVEFORMS,
            producer="t",
            stream_time_utc=chunk.start_time_utc,
            replay_run_id="run-1",
        )
        bus.publish(env)
    while runner.step():
        pass
    detections = bus.log(topics.DETECTIONS)
    assert len(detections) == 1
    envelope = detections[0]
    assert envelope.schema_name == "detection-candidate"
    assert envelope.replay_run_id == "run-1"
    assert envelope.causation_id is not None
    assert isinstance(envelope.payload, DetectionCandidate)
    assert bus.pending(topics.WAVEFORMS, det.group) == 0


def test_rejects_wrong_payload_type() -> None:
    from serac.domain.force_history import ForceHistory
    from serac.ports.bus import Received

    env: Envelope[BaseModel] = wrap(
        ForceHistory(), topic=topics.WAVEFORMS, producer="t", stream_time_utc=T0
    )
    with pytest.raises(detector_stub.DetectorStubError, match="expected SeismicTrace"):
        DetectorStub().process(Received(message_id="1-0", topic=topics.WAVEFORMS, envelope=env))


def test_chunk_type_is_seismic_trace() -> None:
    chunk = next(synthetic_chunks(start_utc=T0, n_chunks=1))
    assert isinstance(chunk, SeismicTrace)
