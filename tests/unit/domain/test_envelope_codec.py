from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from serac.domain import topics
from serac.domain.cap import CAPInfo, CAPMessage
from serac.domain.codec import (
    SCHEMA_REGISTRY,
    CodecError,
    decode,
    encode,
    spec_for,
    wrap,
)
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import CONTRACTS, Envelope
from serac.domain.force_history import ForceHistory
from serac.domain.seismic import (
    SeismicTrace,
    Sncl,
    TraceEncoding,
    TraceProvenance,
    TraceSource,
    sha256_of_bytes,
)

T0 = datetime(2026, 8, 26, 2, 52, tzinfo=UTC)


def trace() -> SeismicTrace:
    data = bytes(range(256)) * 4
    return SeismicTrace(
        trace_id="t",
        sncl=Sncl.from_key("NK.KKN..BHZ"),
        start_time_utc=T0,
        end_time_utc=T0 + timedelta(seconds=255 / 50),
        sampling_rate_hz=50,
        npts=256,
        encoding=TraceEncoding.float32le,
        data=data,
        data_sha256=sha256_of_bytes(data),
        sequence=3,
        provenance=TraceProvenance(source=TraceSource.synthetic, notes="codec test"),
    )


def detection() -> DetectionCandidate:
    return DetectionCandidate(
        detection_id="d1",
        sncl=Sncl.from_key("NK.KKN..BHZ"),
        detector="lp-sp-ratio-stub",
        detector_version="0.0.1",
        window_start_utc=T0,
        window_end_utc=T0 + timedelta(seconds=120),
        detected_at_stream_utc=T0 + timedelta(seconds=120),
        score=12.5,
        threshold=10.0,
    )


def cap() -> CAPMessage:
    return CAPMessage(
        identifier="serac-stub-1",
        sender="serac-stub@serac.invalid",
        sent=T0,
        status="Test",
        msg_type="Alert",
        scope="Private",
        addresses="serac-dev",
        info=[
            CAPInfo(
                category=["Geo"],
                event="Long-period seismic signal (stub)",
                urgency="Unknown",
                severity="Unknown",
                certainty="Unknown",
            )
        ],
    )


class TestWrap:
    def test_fills_schema_from_registry(self) -> None:
        env = wrap(trace(), topic=topics.WAVEFORMS, producer="test", stream_time_utc=T0)
        assert env.schema_name == "seismic-trace"
        assert env.schema_version == SCHEMA_REGISTRY["seismic-trace"].version
        assert env.stream_time_utc == T0
        assert env.produced_at_utc.tzinfo is not None
        assert len(env.message_id) == 32

    def test_unregistered_payload_rejected(self) -> None:
        class Other(BaseModel):
            x: int = 1

        with pytest.raises(CodecError, match="not registered"):
            wrap(Other(), topic="t", producer="p", stream_time_utc=T0)
        with pytest.raises(CodecError):
            spec_for(Other())

    def test_explicit_produced_at(self) -> None:
        env = wrap(
            trace(),
            topic=topics.WAVEFORMS,
            producer="p",
            stream_time_utc=T0,
            produced_at_utc=T0 + timedelta(hours=1),
        )
        assert env.produced_at_utc == T0 + timedelta(hours=1)


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("payload", "topic"),
        [
            (trace(), topics.WAVEFORMS),
            (detection(), topics.DETECTIONS),
            (cap(), topics.ALERTS),
            (ForceHistory(), topics.DETECTIONS),
        ],
        ids=["seismic-trace", "detection-candidate", "cap-message", "force-history"],
    )
    def test_encode_decode(self, payload: BaseModel, topic: str) -> None:
        env = wrap(
            payload,
            topic=topic,
            producer="p",
            stream_time_utc=T0,
            causation_id="abc",
            replay_run_id="run-1",
        )
        raw = encode(env)
        back = decode(raw)
        assert isinstance(back, Envelope)
        assert type(back.payload) is type(payload)
        assert back.payload == payload
        assert back.message_id == env.message_id
        assert back.causation_id == "abc"
        assert back.replay_run_id == "run-1"
        assert back == env

    def test_bytes_travel_as_base64(self) -> None:
        raw = encode(wrap(trace(), topic=topics.WAVEFORMS, producer="p", stream_time_utc=T0))
        doc = json.loads(raw)
        assert isinstance(doc["payload"]["data"], str)
        assert decode(raw).payload == trace()


class TestRejections:
    def test_unknown_schema_name_on_encode(self) -> None:
        env = Envelope[SeismicTrace](
            topic="t",
            schema_name="not-a-schema",
            schema_version="0.1.0",
            producer="p",
            stream_time_utc=T0,
            payload=trace(),
        )
        with pytest.raises(CodecError, match="unknown schema"):
            encode(env)

    def test_payload_model_mismatch_on_encode(self) -> None:
        env = Envelope[SeismicTrace](
            topic="t",
            schema_name="cap-message",
            schema_version="0.1.0",
            producer="p",
            stream_time_utc=T0,
            payload=trace(),
        )
        with pytest.raises(CodecError, match="expects CAPMessage"):
            encode(env)

    def test_major_version_mismatch_on_encode_and_decode(self) -> None:
        good = wrap(trace(), topic="t", producer="p", stream_time_utc=T0)
        bad = good.model_copy(update={"schema_version": "9.0.0"})
        with pytest.raises(CodecError, match="major version"):
            encode(bad)
        doc = json.loads(encode(good))
        doc["schema_version"] = "9.0.0"
        with pytest.raises(CodecError, match="major version"):
            decode(json.dumps(doc).encode())

    def test_minor_version_difference_is_accepted(self) -> None:
        doc = json.loads(encode(wrap(trace(), topic="t", producer="p", stream_time_utc=T0)))
        doc["schema_version"] = "0.99.0"
        assert decode(json.dumps(doc).encode()).schema_version == "0.99.0"

    def test_unknown_schema_name_on_decode(self) -> None:
        doc = json.loads(encode(wrap(trace(), topic="t", producer="p", stream_time_utc=T0)))
        doc["schema_name"] = "mystery"
        with pytest.raises(CodecError, match="unknown schema"):
            decode(json.dumps(doc).encode())

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"topic": "t"}'])
    def test_malformed_envelope(self, raw: bytes) -> None:
        with pytest.raises(CodecError, match="malformed"):
            decode(raw)

    def test_invalid_payload_on_decode(self) -> None:
        doc = json.loads(encode(wrap(trace(), topic="t", producer="p", stream_time_utc=T0)))
        doc["payload"]["npts"] = 7  # breaks end-time and length invariants
        with pytest.raises(CodecError, match="does not satisfy"):
            decode(json.dumps(doc).encode())

    def test_envelope_rejects_bad_version_string(self) -> None:
        with pytest.raises(ValidationError):
            Envelope[SeismicTrace](
                topic="t",
                schema_name="seismic-trace",
                schema_version="v1",
                producer="p",
                stream_time_utc=T0,
                payload=trace(),
            )


def test_registry_contents() -> None:
    assert set(SCHEMA_REGISTRY) == {
        "seismic-trace",
        "detection-candidate",
        "force-history",
        "cap-message",
    }
    for name, spec in SCHEMA_REGISTRY.items():
        assert spec.name == name
        assert spec.major == int(spec.version.split(".")[0])
    assert {"envelope": Envelope} == CONTRACTS
    assert topics.ALL_TOPICS == (topics.WAVEFORMS, topics.DETECTIONS, topics.ALERTS)
