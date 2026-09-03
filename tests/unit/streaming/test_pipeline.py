from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from serac.adapters.bus.in_memory import InMemoryBus
from serac.domain import topics
from serac.domain.cap import CAPInfo, CAPMessage
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace
from serac.ports.bus import Received
from serac.ports.clock import VirtualClock, WallClock
from serac.streaming.pipeline import Pipeline, PipelineError
from serac.streaming.stage import Stage, StageRunner
from serac.streaming.synthetic import synthetic_chunks

T0 = datetime(2026, 8, 26, 2, 50, tzinfo=UTC)


class EveryOtherChunkDetects(Stage):
    """Test stage: emits a detection for every even-sequence chunk."""

    name = "detector-test"
    input_topic = topics.WAVEFORMS
    group = "detector"

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        chunk = received.envelope.payload
        assert isinstance(chunk, SeismicTrace)
        if chunk.sequence % 2:
            return []
        det = DetectionCandidate(
            detection_id=f"det-{chunk.sequence}",
            sncl=chunk.sncl,
            detector="test",
            detector_version="0",
            window_start_utc=chunk.start_time_utc,
            window_end_utc=chunk.end_time_utc,
            detected_at_stream_utc=chunk.end_time_utc,
            score=1.0,
            threshold=0.5,
            input_trace_ids=[chunk.trace_id],
        )
        return [
            wrap(
                det,
                topic=topics.DETECTIONS,
                producer=self.name,
                stream_time_utc=det.detected_at_stream_utc,
                causation_id=received.envelope.message_id,
            )
        ]


class DetectionToCap(Stage):
    name = "cap-test"
    input_topic = topics.DETECTIONS
    group = "cap"

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        det = received.envelope.payload
        assert isinstance(det, DetectionCandidate)
        msg = CAPMessage(
            identifier=f"cap-{det.detection_id}",
            sender="serac-stub@serac.invalid",
            sent=det.detected_at_stream_utc,
            status="Test",
            msg_type="Alert",
            scope="Private",
            addresses="test",
            info=[
                CAPInfo(
                    category=["Geo"],
                    event="test",
                    urgency="Unknown",
                    severity="Unknown",
                    certainty="Unknown",
                )
            ],
        )
        return [
            wrap(
                msg,
                topic=topics.ALERTS,
                producer=self.name,
                stream_time_utc=received.envelope.stream_time_utc,
                causation_id=received.envelope.message_id,
            )
        ]


class Echo(Stage):
    """Re-publishes every waveform chunk to its own input: never settles."""

    name = "echo"
    input_topic = topics.WAVEFORMS
    group = "echo"

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        return [received.envelope.model_copy(update={"message_id": "x" + received.message_id})]


def publish_chunks(bus: InMemoryBus, n: int) -> None:
    for chunk in synthetic_chunks(start_utc=T0, n_chunks=n, chunk_seconds=1, sampling_rate_hz=20):
        bus.publish(
            wrap(
                chunk, topic=topics.WAVEFORMS, producer="src", stream_time_utc=chunk.start_time_utc
            )
        )


class TestPipeline:
    def test_drain_runs_the_chain_to_quiescence(self) -> None:
        bus = InMemoryBus()
        pipeline = Pipeline(bus, [EveryOtherChunkDetects(), DetectionToCap()])
        publish_chunks(bus, 10)
        result = pipeline.drain()
        assert result.processed == {"detector-test": 10, "cap-test": 5}
        assert result.published == {"detector-test": 5, "cap-test": 5}
        assert result.pending == {"detector-test": 0, "cap-test": 0}
        assert result.total_pending == 0
        assert result.rounds == 2  # one round of work, one empty round proves quiescence
        alerts = bus.log(topics.ALERTS)
        assert len(alerts) == 5
        detections = {e.message_id: e for e in bus.log(topics.DETECTIONS)}
        for alert in alerts:
            assert alert.causation_id in detections
            assert detections[alert.causation_id].stream_time_utc == alert.stream_time_utc

    def test_drain_is_deterministic(self) -> None:
        runs = []
        for _ in range(2):
            bus = InMemoryBus()
            publish_chunks(bus, 6)
            Pipeline(bus, [EveryOtherChunkDetects(), DetectionToCap()]).drain()
            runs.append([e.payload.identifier for e in bus.log(topics.ALERTS)])  # type: ignore[attr-defined]
        assert runs[0] == runs[1] == ["cap-det-0", "cap-det-2", "cap-det-4"]

    def test_downstream_first_order_needs_more_rounds_but_still_settles(self) -> None:
        bus = InMemoryBus()
        publish_chunks(bus, 4)
        result = Pipeline(bus, [DetectionToCap(), EveryOtherChunkDetects()]).drain()
        assert result.rounds == 3
        assert len(bus.log(topics.ALERTS)) == 2

    def test_batch_size_limits_work_per_round(self) -> None:
        bus = InMemoryBus()
        publish_chunks(bus, 7)
        result = Pipeline(bus, [EveryOtherChunkDetects()], batch=3).drain()
        assert result.rounds == 4
        assert result.processed["detector-test"] == 7

    def test_max_rounds_guard(self) -> None:
        bus = InMemoryBus()
        publish_chunks(bus, 1)
        with pytest.raises(PipelineError, match="did not settle"):
            Pipeline(bus, [Echo()]).drain(max_rounds=5)

    def test_rejects_empty_or_duplicate_stages(self) -> None:
        bus = InMemoryBus()
        with pytest.raises(PipelineError):
            Pipeline(bus, [])
        with pytest.raises(PipelineError, match="unique"):
            Pipeline(bus, [Echo(), Echo()])


class TestStageRunner:
    def test_step_acks_after_publishing(self) -> None:
        bus = InMemoryBus()
        runner = StageRunner(bus, EveryOtherChunkDetects(), batch=2)
        publish_chunks(bus, 3)
        assert runner.step() == 2
        assert bus.pending(topics.WAVEFORMS, "detector") == 0
        assert len(bus.log(topics.DETECTIONS)) == 1
        assert runner.step() == 1
        assert runner.step() == 0
        assert runner.processed == 3

    def test_run_forever_sleeps_when_idle_and_stops(self) -> None:
        bus = InMemoryBus()
        publish_chunks(bus, 2)
        runner = StageRunner(bus, EveryOtherChunkDetects())
        clock = VirtualClock(T0)
        steps = {"n": 0}

        def should_stop() -> bool:
            steps["n"] += 1
            return steps["n"] > 3

        processed = runner.run_forever(clock=clock, idle_sleep_s=0.25, should_stop=should_stop)
        assert processed == 2
        assert clock.sleeps == [0.25, 0.25]
        assert clock.now() == T0 + timedelta(seconds=0.5)


class TestClocks:
    def test_virtual_clock_records_and_advances(self) -> None:
        clock = VirtualClock(T0)
        clock.sleep(1.5)
        clock.sleep(-3)
        clock.sleep_until(T0 + timedelta(seconds=10))
        assert clock.sleeps == [1.5, -3, 8.5]
        assert clock.total_slept_s == 10
        assert clock.now() == T0 + timedelta(seconds=10)
        clock.advance(2)
        assert clock.now() == T0 + timedelta(seconds=12)
        assert clock.sleeps == [1.5, -3, 8.5]
        clock.sleep_until(T0)
        assert clock.now() == T0 + timedelta(seconds=12)

    def test_virtual_clock_rejects_naive_and_backwards(self) -> None:
        with pytest.raises(ValueError):
            VirtualClock(datetime(2020, 1, 1))
        with pytest.raises(ValueError):
            VirtualClock(T0).advance(-1)

    def test_wall_clock(self) -> None:
        clock = WallClock()
        before = clock.now()
        clock.sleep(0)
        clock.sleep(-1)
        assert before.tzinfo is UTC
        assert clock.now() >= before
