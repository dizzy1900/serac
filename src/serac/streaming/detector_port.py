"""Adapters between the `Detector` port and the concrete detectors.

`StubDetector` wraps the Prompt 1 placeholder without changing a line of it: `ingest` runs
the same `evaluate` call and stashes whatever it returns, `poll` hands it over. Behaviour is
bit-for-bit identical, so `streaming/golden.py`'s pinned ratio record and every existing
stream check remain valid while the port takes over the plumbing.
"""

from __future__ import annotations

from datetime import datetime

from serac.domain.detection import DetectionCandidate
from serac.domain.seismic import SeismicTrace
from serac.ports.detector import Detector, DetectorInfo
from serac.streaming.detector_stub import (
    DETECTOR_NAME,
    DETECTOR_VERSION,
    DetectorStub,
    DetectorStubConfig,
)


class StubDetector(Detector):
    """The Prompt 1 placeholder, behind the port."""

    def __init__(self, config: DetectorStubConfig | None = None) -> None:
        self.stub = DetectorStub(config or DetectorStubConfig())
        self._pending: list[DetectionCandidate] = []

    def info(self) -> DetectorInfo:
        params = self.stub.config.as_params()
        return DetectorInfo(
            name=DETECTOR_NAME,
            version=DETECTOR_VERSION,
            is_stub=True,
            params=params,
        )

    def ingest(self, chunk: SeismicTrace) -> None:
        candidate = self.stub.evaluate(chunk)
        if candidate is not None:
            self._pending.append(candidate)

    def poll(self, stream_time_utc: datetime) -> list[DetectionCandidate]:
        ready, self._pending = self._pending, []
        return ready

    def reset(self) -> None:
        self.stub = DetectorStub(self.stub.config)
        self._pending = []
