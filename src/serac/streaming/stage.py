"""Processing stages over the message bus.

A `Stage` consumes one topic through a consumer group and may publish to another. The
`StageRunner` owns the read → process → publish → ack loop; acknowledgement happens after
the outputs are published, so a crash mid-stage redelivers (at-least-once) rather than
losing a message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from pydantic import BaseModel

from serac.domain.envelope import Envelope
from serac.ports.bus import MessageBus, Received
from serac.ports.clock import Clock, WallClock


class Stage(ABC):
    """A pure-ish transformation from received envelopes to envelopes to publish."""

    name: str
    input_topic: str
    group: str

    @abstractmethod
    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        """Handle one delivery; return envelopes to publish (possibly none)."""


class StageRunner:
    """Drives one stage against one bus."""

    def __init__(
        self, bus: MessageBus, stage: Stage, *, consumer: str = "c0", batch: int = 100
    ) -> None:
        self.bus = bus
        self.stage = stage
        self.consumer = consumer
        self.batch = batch
        self.processed = 0
        self.published = 0
        bus.ensure_group(stage.input_topic, stage.group)

    def step(self, *, block_ms: int = 0) -> int:
        """Process one batch; return how many deliveries were handled."""
        deliveries = self.bus.read(
            self.stage.input_topic,
            self.stage.group,
            self.consumer,
            count=self.batch,
            block_ms=block_ms,
        )
        for received in deliveries:
            outputs = self.stage.process(received)
            for envelope in outputs:
                self.bus.publish(envelope)
            self.published += len(outputs)
            self.bus.ack(self.stage.input_topic, self.stage.group, [received.message_id])
            self.processed += 1
        return len(deliveries)

    def run_forever(
        self,
        *,
        clock: Clock | None = None,
        idle_sleep_s: float = 0.1,
        block_ms: int = 1000,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Loop until `should_stop()` is true; return total deliveries processed."""
        clock = clock or WallClock()
        while not (should_stop and should_stop()):
            if self.step(block_ms=block_ms) == 0:
                clock.sleep(idle_sleep_s)
        return self.processed
