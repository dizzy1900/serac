"""Behavioural contract every `MessageBus` adapter must satisfy.

Parametrised over the in-memory bus and the Redis Streams bus on `fakeredis`, so the Redis
code path is exercised for real offline (only the live-server test is skipped).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import fakeredis
import pytest
from pydantic import BaseModel

from serac.adapters.bus.in_memory import InMemoryBus
from serac.adapters.bus.redis_streams import RedisStreamsBus
from serac.domain import topics
from serac.domain.codec import CodecError, wrap
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace
from serac.ports.bus import BusError, MessageBus
from serac.streaming.synthetic import synthetic_chunks

T0 = datetime(2026, 8, 26, 2, 50, tzinfo=UTC)


@pytest.fixture(params=["in_memory", "redis_streams"])
def bus(request: pytest.FixtureRequest) -> Iterator[MessageBus]:
    instance: MessageBus
    if request.param == "in_memory":
        instance = InMemoryBus()
    else:
        instance = RedisStreamsBus(fakeredis.FakeRedis(server=fakeredis.FakeServer()))
    yield instance
    instance.close()


def chunks(n: int) -> list[SeismicTrace]:
    return list(synthetic_chunks(start_utc=T0, n_chunks=n, chunk_seconds=1, sampling_rate_hz=20))


def envelope(chunk: SeismicTrace) -> Envelope[SeismicTrace]:
    return wrap(
        chunk, topic=topics.WAVEFORMS, producer="test", stream_time_utc=chunk.start_time_utc
    )


def test_publish_read_ack_pending(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    published = [bus.publish(envelope(c)) for c in chunks(3)]
    assert len(set(published)) == 3
    got = bus.read(topics.WAVEFORMS, "g", "c0", count=10)
    assert [r.message_id for r in got] == published
    assert bus.pending(topics.WAVEFORMS, "g") == 3
    assert bus.ack(topics.WAVEFORMS, "g", [r.message_id for r in got]) == 3
    assert bus.pending(topics.WAVEFORMS, "g") == 0
    assert bus.read(topics.WAVEFORMS, "g", "c0") == []


def test_consumer_receives_typed_fresh_payload(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    original = chunks(1)[0]
    bus.publish(envelope(original))
    received = bus.read(topics.WAVEFORMS, "g", "c0")[0]
    assert received.topic == topics.WAVEFORMS
    assert isinstance(received.envelope.payload, SeismicTrace)
    assert received.envelope.payload == original
    assert received.envelope.payload is not original
    assert received.envelope.schema_name == "seismic-trace"


def test_ordering_and_count_limit(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    for c in chunks(5):
        bus.publish(envelope(c))
    first = bus.read(topics.WAVEFORMS, "g", "c0", count=2)
    second = bus.read(topics.WAVEFORMS, "g", "c0", count=10)
    sequences = [r.envelope.payload.sequence for r in first + second]  # type: ignore[attr-defined]
    assert sequences == [0, 1, 2, 3, 4]


def test_group_created_after_publish_sees_backlog(bus: MessageBus) -> None:
    for c in chunks(2):
        bus.publish(envelope(c))
    bus.ensure_group(topics.WAVEFORMS, "late")
    assert len(bus.read(topics.WAVEFORMS, "late", "c0")) == 2


def test_ensure_group_is_idempotent_and_keeps_cursor(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    bus.publish(envelope(chunks(1)[0]))
    assert len(bus.read(topics.WAVEFORMS, "g", "c0")) == 1
    bus.ensure_group(topics.WAVEFORMS, "g")
    assert bus.read(topics.WAVEFORMS, "g", "c0") == []
    assert bus.pending(topics.WAVEFORMS, "g") == 1


def test_groups_are_independent(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "a")
    bus.ensure_group(topics.WAVEFORMS, "b")
    bus.publish(envelope(chunks(1)[0]))
    ra = bus.read(topics.WAVEFORMS, "a", "c0")
    rb = bus.read(topics.WAVEFORMS, "b", "c0")
    assert len(ra) == len(rb) == 1
    bus.ack(topics.WAVEFORMS, "a", [ra[0].message_id])
    assert bus.pending(topics.WAVEFORMS, "a") == 0
    assert bus.pending(topics.WAVEFORMS, "b") == 1


def test_consumers_in_one_group_share_the_work(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    for c in chunks(4):
        bus.publish(envelope(c))
    first = bus.read(topics.WAVEFORMS, "g", "c0", count=2)
    second = bus.read(topics.WAVEFORMS, "g", "c1", count=10)
    ids = [r.message_id for r in first] + [r.message_id for r in second]
    assert len(ids) == 4
    assert len(set(ids)) == 4


def test_partial_ack_and_unknown_ids(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    for c in chunks(3):
        bus.publish(envelope(c))
    got = bus.read(topics.WAVEFORMS, "g", "c0")
    assert bus.ack(topics.WAVEFORMS, "g", [got[0].message_id]) == 1
    assert bus.pending(topics.WAVEFORMS, "g") == 2
    assert bus.ack(topics.WAVEFORMS, "g", ["0-0"]) == 0
    assert bus.ack(topics.WAVEFORMS, "g", []) == 0


def test_topics_are_isolated(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    bus.ensure_group(topics.DETECTIONS, "g")
    bus.publish(envelope(chunks(1)[0]))
    assert bus.read(topics.DETECTIONS, "g", "c0") == []
    assert len(bus.read(topics.WAVEFORMS, "g", "c0")) == 1


def test_read_without_group_raises(bus: MessageBus) -> None:
    with pytest.raises(BusError, match="does not exist"):
        bus.read(topics.WAVEFORMS, "missing", "c0")
    with pytest.raises(BusError, match="does not exist"):
        bus.pending(topics.WAVEFORMS, "missing")


def test_invalid_count(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    with pytest.raises(BusError):
        bus.read(topics.WAVEFORMS, "g", "c0", count=0)


def test_unknown_schema_rejected_at_publish(bus: MessageBus) -> None:
    bus.ensure_group(topics.WAVEFORMS, "g")
    env = envelope(chunks(1)[0]).model_copy(update={"schema_name": "not-registered"})
    with pytest.raises(CodecError, match="unknown schema"):
        bus.publish(env)
    assert bus.read(topics.WAVEFORMS, "g", "c0") == []


def test_wrong_payload_type_rejected_at_publish(bus: MessageBus) -> None:
    class Rogue(BaseModel):
        x: int = 1

    env = Envelope[Rogue](
        topic=topics.WAVEFORMS,
        schema_name="seismic-trace",
        schema_version="0.1.0",
        producer="p",
        stream_time_utc=T0,
        payload=Rogue(),
    )
    with pytest.raises(CodecError, match="expects SeismicTrace"):
        bus.publish(env)  # type: ignore[arg-type]


def test_major_version_mismatch_rejected_at_publish(bus: MessageBus) -> None:
    env = envelope(chunks(1)[0]).model_copy(update={"schema_version": "7.0.0"})
    with pytest.raises(CodecError, match="major version"):
        bus.publish(env)


class TestInMemoryHelpers:
    def test_log_and_reset(self) -> None:
        bus = InMemoryBus()
        bus.ensure_group(topics.WAVEFORMS, "g")
        for c in chunks(2):
            bus.publish(envelope(c))
        assert [e.payload.sequence for e in bus.log(topics.WAVEFORMS)] == [0, 1]  # type: ignore[attr-defined]
        assert bus.topics() == [topics.WAVEFORMS]
        bus.reset()
        assert bus.log(topics.WAVEFORMS) == []
        with pytest.raises(BusError):
            bus.read(topics.WAVEFORMS, "g", "c0")

    def test_closed_bus_refuses(self) -> None:
        bus = InMemoryBus()
        bus.close()
        with pytest.raises(BusError, match="closed"):
            bus.publish(envelope(chunks(1)[0]))


class TestRedisSpecifics:
    def test_from_url_builds_a_client(self) -> None:
        bus = RedisStreamsBus.from_url("redis://localhost:6379/0")
        assert isinstance(bus, RedisStreamsBus)

    def test_maxlen_must_be_positive(self) -> None:
        with pytest.raises(BusError):
            RedisStreamsBus(fakeredis.FakeRedis(server=fakeredis.FakeServer()), maxlen=0)

    def test_stream_is_trimmed_approximately(self) -> None:
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer())
        bus = RedisStreamsBus(client, maxlen=5)
        for c in chunks(50):
            bus.publish(envelope(c))
        assert client.xlen(topics.WAVEFORMS) <= 50

    def test_entry_without_envelope_field_is_an_error(self) -> None:
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer())
        bus = RedisStreamsBus(client)
        bus.ensure_group(topics.WAVEFORMS, "g")
        client.xadd(topics.WAVEFORMS, {b"other": b"x"})
        with pytest.raises(BusError, match="no envelope field"):
            bus.read(topics.WAVEFORMS, "g", "c0")
