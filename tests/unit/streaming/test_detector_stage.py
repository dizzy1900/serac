"""The Detector port must drive the same lane as the stub, so either can be selected."""

from __future__ import annotations

from datetime import datetime

import pytest

from serac.domain.detection import DetectionCandidate
from serac.domain.seismic import SeismicTrace
from serac.ports.detector import Detector, DetectorInfo
from serac.streaming.detector_port import StubDetector
from serac.streaming.detector_stage import DetectorStage, DetectorStageError


class _Silent(Detector):
    """A detector that accumulates and never fires, which is a legitimate outcome."""

    def __init__(self) -> None:
        self.ingested = 0

    def info(self) -> DetectorInfo:
        return DetectorInfo(name="silent", version="0.0.0", is_stub=False)

    def ingest(self, chunk: SeismicTrace) -> None:
        self.ingested += 1

    def poll(self, stream_time_utc: datetime) -> list[DetectionCandidate]:
        return []

    def reset(self) -> None:
        self.ingested = 0


def test_the_stage_names_itself_after_its_detector() -> None:
    # The Pipeline enforces unique stage names, and a replay report must say which detector
    # produced it, so the name cannot be a constant.
    assert DetectorStage(StubDetector()).name == "detector-lp-sp-ratio-stub"
    assert DetectorStage(_Silent()).name == "detector-silent"


def test_a_detector_that_never_fires_publishes_nothing() -> None:
    stage = DetectorStage(_Silent())
    assert stage.detections == 0
    assert stage.input_topic == "serac.waveforms"


class _NotAChunk:
    """Shaped like a Received, carrying the wrong payload."""

    class envelope:  # noqa: N801 - test double, not a real class name
        payload = "not a chunk"
        message_id = "m"
        replay_run_id = None


def test_a_non_waveform_payload_is_refused() -> None:
    stage = DetectorStage(_Silent())
    with pytest.raises(DetectorStageError, match="expected SeismicTrace"):
        stage.process(_NotAChunk())  # type: ignore[arg-type]
