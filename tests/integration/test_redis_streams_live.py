"""Live Redis Streams check. Needs `SERAC_REDIS_URL`; skipped otherwise (see conftest)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import redis

from serac.adapters.bus.redis_streams import RedisStreamsBus
from serac.domain.codec import wrap
from serac.streaming.synthetic import synthetic_chunks

pytestmark = pytest.mark.redis


def test_publish_read_ack_on_live_server() -> None:
    url = os.environ["SERAC_REDIS_URL"]
    topic = f"serac.test.{uuid.uuid4().hex}"
    client = redis.Redis.from_url(url)
    bus = RedisStreamsBus(client, maxlen=100)
    try:
        bus.ensure_group(topic, "g")
        chunks = list(
            synthetic_chunks(
                start_utc=datetime(2026, 1, 1, tzinfo=UTC), n_chunks=3, chunk_seconds=1
            )
        )
        for chunk in chunks:
            bus.publish(
                wrap(chunk, topic=topic, producer="live-test", stream_time_utc=chunk.start_time_utc)
            )
        got = bus.read(topic, "g", "c0", count=10, block_ms=500)
        assert [r.envelope.payload for r in got] == chunks
        assert bus.pending(topic, "g") == 3
        assert bus.ack(topic, "g", [r.message_id for r in got]) == 3
        assert bus.pending(topic, "g") == 0
    finally:
        client.delete(topic)
        bus.close()
