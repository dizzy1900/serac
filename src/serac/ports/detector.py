"""Port for anything that turns waveform chunks into detection candidates.

The Prompt 1 stub decided per chunk on one channel. A trained discriminator needs several
stations before it can say anything, so the port is `ingest` then `poll` rather than
`evaluate(chunk) -> candidate | None`: chunks accumulate, and the stage asks for candidates
whenever it wants them. `DetectorStub` is wrapped to this shape without changing its
behaviour, so the golden ratio record and every existing stream check stay valid.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from serac.domain.detection import DetectionCandidate
from serac.domain.seismic import SeismicTrace

DETECTOR_PORT_VERSION = "0.1.0"


class DetectorInfo(BaseModel):
    """What a detector says about itself, recorded in every replay report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    is_stub: bool = Field(description="True only for a placeholder with no validated skill.")
    model_sha256: str | None = None
    calibration: str | None = None
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)


class Detector(ABC):
    """Accumulates waveform chunks and emits candidates when it has enough to speak."""

    @abstractmethod
    def info(self) -> DetectorInfo:
        """Identify the detector and its parameters."""

    @abstractmethod
    def ingest(self, chunk: SeismicTrace) -> None:
        """Take one chunk into the detector's buffers."""

    @abstractmethod
    def poll(self, stream_time_utc: datetime) -> list[DetectionCandidate]:
        """Return any candidates that are ready as of `stream_time_utc` (often none)."""

    @abstractmethod
    def reset(self) -> None:
        """Drop all buffered state."""
