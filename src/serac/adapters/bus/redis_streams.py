"""Redis Streams implementation of the message bus.

One stream per topic; each entry holds the codec bytes under the `envelope` field. Consumer
groups map one-to-one onto Redis consumer groups (created with `MKSTREAM` from id `0` so a
group created after publishing still sees the backlog, which replay relies on). Streams are
capped with `XADD MAXLEN ~ maxlen` so a long-running feed cannot grow without bound.

Tested offline against `fakeredis`; a live server test is `redis`-marked and skipped here.
The redis-py client annotates every command as `ResponseT = Awaitable[Any] | Any` because the
same signatures serve the async client. `_sync` is the single place that narrows those values
for the synchronous client; nothing else in this module casts or ignores.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Self

import redis
from pydantic import BaseModel

from serac.domain import codec
from serac.domain.envelope import Envelope
from serac.ports.bus import BusError, MessageBus, Received

ENVELOPE_FIELD = b"envelope"


def _sync(value: Any) -> Any:
    """Narrow redis-py's `ResponseT` for the synchronous client.

    The sync `redis.Redis` never returns an awaitable; if one appears the client was
    misconfigured, and we fail loudly rather than return a coroutine to the caller.
    """
    if isinstance(value, Awaitable):
        raise BusError("received an awaitable from the synchronous Redis client")
    return value


def _as_str(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _is_missing_group(exc: redis.ResponseError) -> bool:
    """Redis says `NOGROUP ...`; fakeredis says the XGROUP key must exist. Same meaning."""
    text = str(exc)
    return "NOGROUP" in text or "requires the key to exist" in text


class RedisStreamsBus(MessageBus):
    """`MessageBus` over Redis Streams (XADD / XGROUP / XREADGROUP / XACK / XPENDING)."""

    def __init__(self, client: redis.Redis, *, maxlen: int = 100_000) -> None:
        if maxlen < 1:
            raise BusError("maxlen must be positive")
        self._client = client
        self._maxlen = maxlen

    @classmethod
    def from_url(cls, url: str, *, maxlen: int = 100_000) -> Self:
        """Connect with `redis.Redis.from_url`; the URL is `SERAC_REDIS_URL` in settings."""
        return cls(redis.Redis.from_url(url), maxlen=maxlen)

    def publish(self, envelope: Envelope[BaseModel]) -> str:
        raw = codec.encode(envelope)
        try:
            message_id = _sync(
                self._client.xadd(
                    envelope.topic, {ENVELOPE_FIELD: raw}, maxlen=self._maxlen, approximate=True
                )
            )
        except redis.RedisError as exc:
            raise BusError(f"XADD {envelope.topic} failed: {exc}") from exc
        return _as_str(message_id)

    def ensure_group(self, topic: str, group: str) -> None:
        try:
            _sync(self._client.xgroup_create(topic, group, id="0", mkstream=True))
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise BusError(f"XGROUP CREATE {topic} {group} failed: {exc}") from exc
        except redis.RedisError as exc:
            raise BusError(f"XGROUP CREATE {topic} {group} failed: {exc}") from exc

    def read(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Received]:
        if count < 1:
            raise BusError("count must be positive")
        try:
            response = _sync(
                self._client.xreadgroup(
                    group,
                    consumer,
                    {topic: ">"},
                    count=count,
                    block=block_ms if block_ms > 0 else None,
                )
            )
        except redis.ResponseError as exc:
            if _is_missing_group(exc):
                raise BusError(f"consumer group {group!r} does not exist on {topic!r}") from exc
            raise BusError(f"XREADGROUP {topic} {group} failed: {exc}") from exc
        except redis.RedisError as exc:
            raise BusError(f"XREADGROUP {topic} {group} failed: {exc}") from exc
        out: list[Received] = []
        for _stream, entries in response or []:
            for message_id, fields in entries:
                raw = fields.get(ENVELOPE_FIELD)
                if not isinstance(raw, bytes):
                    raise BusError(f"stream entry {_as_str(message_id)} has no envelope field")
                out.append(
                    Received(
                        message_id=_as_str(message_id), topic=topic, envelope=codec.decode(raw)
                    )
                )
        return out

    def ack(self, topic: str, group: str, message_ids: Sequence[str]) -> int:
        if not message_ids:
            return 0
        try:
            acked = _sync(self._client.xack(topic, group, *message_ids))
        except redis.RedisError as exc:
            raise BusError(f"XACK {topic} {group} failed: {exc}") from exc
        return int(acked)

    def pending(self, topic: str, group: str) -> int:
        try:
            summary = _sync(self._client.xpending(topic, group))
        except redis.ResponseError as exc:
            if _is_missing_group(exc):
                raise BusError(f"consumer group {group!r} does not exist on {topic!r}") from exc
            raise BusError(f"XPENDING {topic} {group} failed: {exc}") from exc
        except redis.RedisError as exc:
            raise BusError(f"XPENDING {topic} {group} failed: {exc}") from exc
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        return int(summary[0]) if summary else 0

    def close(self) -> None:
        self._client.close()
