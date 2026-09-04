"""Drive any `Detector` as a pipeline stage.

`DetectorStub` is itself a `Stage` and decides per chunk. A trained multi-station detector
cannot: it needs several receivers before it can say anything, which is why the `Detector`
port is `ingest` then `poll`. This adapter is what lets either kind run in the same lane, so
`serac replay --detector` can select between them without the pipeline knowing the
difference.
"""

from __future__ import annotations

from pydantic import BaseModel

from serac.domain import topics
from serac.domain.codec import wrap
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace
from serac.errors import SeracError
from serac.ports.bus import Received
from serac.ports.detector import Detector
from serac.streaming.stage import Stage


class DetectorStageError(SeracError):
    """The stage received something that is not a waveform chunk."""


class DetectorStage(Stage):
    """`serac.waveforms` -> `serac.detections` for any `Detector` implementation."""

    input_topic = topics.WAVEFORMS
    group = "detector"

    def __init__(self, detector: Detector, *, name: str | None = None) -> None:
        self.detector = detector
        info = detector.info()
        # The stage name carries the detector's identity so a replay report, and the
        # Pipeline's own uniqueness check, distinguish a stub run from a trained one.
        self.name = name or f"detector-{info.name}"
        self.detections = 0
        self.chunks_seen = 0

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        chunk = received.envelope.payload
        if not isinstance(chunk, SeismicTrace):
            raise DetectorStageError(
                f"expected SeismicTrace on {self.input_topic}, got {type(chunk).__name__}"
            )
        self.chunks_seen += 1
        self.detector.ingest(chunk)
        candidates = self.detector.poll(chunk.end_time_utc)
        self.detections += len(candidates)
        return [
            wrap(
                candidate,
                topic=topics.DETECTIONS,
                producer=self.name,
                stream_time_utc=candidate.detected_at_stream_utc,
                causation_id=received.envelope.message_id,
                replay_run_id=received.envelope.replay_run_id,
            )
            for candidate in candidates
        ]
