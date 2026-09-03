"""Message-bus port.

A synchronous, consumer-group style bus modelled on Redis Streams: messages are appended to a
topic, consumer groups track their own cursor, and a delivery stays *pending* until the
consumer acknowledges it. Adapters: `serac.adapters.bus.in_memory.InMemoryBus` (tests,
deterministic replay) and `serac.adapters.bus.redis_streams.RedisStreamsBus` (dev/prod).

Every payload crosses the bus through `serac.domain.codec`, so an adapter never sees an
unvalidated envelope and an unknown schema name is rejected at publish time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from serac.domain.envelope import Envelope
from serac.errors import SeracError


class BusError(SeracError):
    """The bus could not complete an operation."""


@dataclass(frozen=True)
class Received:
    """One delivery to a consumer. `message_id` is the bus's own id, used for `ack`."""

    message_id: str
    topic: str
    envelope: Envelope[BaseModel]
    delivery_count: int = 1


class MessageBus(ABC):
    """Publish/subscribe with consumer groups and explicit acknowledgement."""

    @abstractmethod
    def publish(self, envelope: Envelope[BaseModel]) -> str:
        """Append `envelope` to its topic; return the bus message id.

        Raises `serac.domain.codec.CodecError` when the envelope's schema is unknown, its
        payload does not match the registered model, or its major version disagrees.
        """

    @abstractmethod
    def ensure_group(self, topic: str, group: str) -> None:
        """Create the consumer group if missing; idempotent. Existing groups keep their cursor."""

    @abstractmethod
    def read(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Received]:
        """Deliver up to `count` new messages to `consumer` in `group`.

        Delivered messages become pending for the group until acknowledged. `block_ms` is a
        hint for adapters that can wait; `0` returns immediately.
        """

    @abstractmethod
    def ack(self, topic: str, group: str, message_ids: Sequence[str]) -> int:
        """Acknowledge deliveries; return how many were actually pending."""

    @abstractmethod
    def pending(self, topic: str, group: str) -> int:
        """Number of delivered-but-unacknowledged messages for the group."""

    @abstractmethod
    def close(self) -> None:
        """Release any connection; the bus must not be used afterwards."""
