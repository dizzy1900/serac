"""Names of the message-bus topics used by the real-time lane."""

from __future__ import annotations

from typing import Final

WAVEFORMS: Final = "serac.waveforms"
"""`SeismicTrace` chunks from SeedLink or replay."""

DETECTIONS: Final = "serac.detections"
"""`DetectionCandidate` records emitted by the detector."""

ALERTS: Final = "serac.alerts"
"""`CAPMessage` records emitted by the CAP stage."""

ALL_TOPICS: Final[tuple[str, ...]] = (WAVEFORMS, DETECTIONS, ALERTS)
