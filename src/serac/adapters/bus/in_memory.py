"""In-process message bus for tests and deterministic replay.

Every `publish` round-trips the envelope through `serac.domain.codec` (encode then decode),
so the in-memory bus rejects exactly what the Redis bus would and consumers receive fresh
objects, never the producer's instance.

Not thread-safe: `Pipeline.drain` runs all stages in one thread by design. Use
`RedisStreamsBus` for anything concurrent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from serac.domain import codec
from serac.domain.envelope import Envelope
from serac.ports.bus import BusError, MessageBus, Received


@dataclass
class _Group:
    cursor: int = 0  # index into the topic log of the next undelivered message
    pending: dict[str, int] = field(default_factory=dict)  # message id -> delivery count


class InMemoryBus(MessageBus):
    """Append-only per-topic logs with consumer-group cursors."""

    def __init__(self) -> None:
        self._logs: dict[str, list[tuple[str, bytes]]] = {}
        self._groups: dict[tuple[str, str], _Group] = {}
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise BusError("bus is closed")

    def publish(self, envelope: Envelope[BaseModel]) -> str:
        self._check_open()
        raw = codec.encode(envelope)
        codec.decode(raw)  # fail here, not at the consumer
        log = self._logs.setdefault(envelope.topic, [])
        message_id = f"{len(log) + 1}-0"
        log.append((message_id, raw))
        return message_id

    def ensure_group(self, topic: str, group: str) -> None:
        self._check_open()
        self._logs.setdefault(topic, [])
        self._groups.setdefault((topic, group), _Group())

    def read(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Received]:
        self._check_open()
        if count < 1:
            raise BusError("count must be positive")
        state = self._groups.get((topic, group))
        if state is None:
            raise BusError(f"consumer group {group!r} does not exist on {topic!r}")
        log = self._logs.get(topic, [])
        out: list[Received] = []
        while state.cursor < len(log) and len(out) < count:
            message_id, raw = log[state.cursor]
            state.cursor += 1
            state.pending[message_id] = state.pending.get(message_id, 0) + 1
            out.append(
                Received(
                    message_id=message_id,
                    topic=topic,
                    envelope=codec.decode(raw),
                    delivery_count=state.pending[message_id],
                )
            )
        return out

    def ack(self, topic: str, group: str, message_ids: Sequence[str]) -> int:
        self._check_open()
        state = self._groups.get((topic, group))
        if state is None:
            raise BusError(f"consumer group {group!r} does not exist on {topic!r}")
        acked = 0
        for message_id in message_ids:
            if state.pending.pop(message_id, None) is not None:
                acked += 1
        return acked

    def pending(self, topic: str, group: str) -> int:
        self._check_open()
        state = self._groups.get((topic, group))
        if state is None:
            raise BusError(f"consumer group {group!r} does not exist on {topic!r}")
        return len(state.pending)

    def close(self) -> None:
        self._closed = True

    # --- test helpers -------------------------------------------------------------------

    def log(self, topic: str) -> list[Envelope[BaseModel]]:
        """Every envelope ever published on `topic`, decoded, in order."""
        return [codec.decode(raw) for _, raw in self._logs.get(topic, [])]

    def topics(self) -> list[str]:
        return sorted(self._logs)

    def reset(self) -> None:
        """Drop all logs and groups; the bus stays open."""
        self._logs.clear()
        self._groups.clear()
