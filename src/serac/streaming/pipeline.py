"""Deterministic single-threaded execution of a chain of stages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from serac.errors import SeracError
from serac.ports.bus import MessageBus
from serac.streaming.stage import Stage, StageRunner


class PipelineError(SeracError):
    """The pipeline did not reach quiescence."""


@dataclass(frozen=True)
class DrainResult:
    """What `Pipeline.drain` did."""

    rounds: int
    processed: dict[str, int] = field(default_factory=dict)
    published: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)

    @property
    def total_pending(self) -> int:
        return sum(self.pending.values())


class Pipeline:
    """Stages run in the given order, one batch each per round, until nothing moves."""

    def __init__(self, bus: MessageBus, stages: Sequence[Stage], *, batch: int = 100) -> None:
        if not stages:
            raise PipelineError("a pipeline needs at least one stage")
        names = [stage.name for stage in stages]
        if len(set(names)) != len(names):
            raise PipelineError(f"stage names must be unique: {names}")
        self.bus = bus
        self.runners = [StageRunner(bus, stage, batch=batch) for stage in stages]

    def drain(self, *, max_rounds: int = 10_000) -> DrainResult:
        """Step every stage until a full round processes nothing.

        Because stages are ordered upstream → downstream, a message published in round *n*
        is consumed in the same round by later stages, so a chain of *k* stages settles in
        at most a handful of rounds regardless of message count.
        """
        rounds = 0
        while True:
            rounds += 1
            if rounds > max_rounds:
                raise PipelineError(f"pipeline did not settle within {max_rounds} rounds")
            moved = sum(runner.step() for runner in self.runners)
            if moved == 0:
                break
        return DrainResult(
            rounds=rounds,
            processed={r.stage.name: r.processed for r in self.runners},
            published={r.stage.name: r.published for r in self.runners},
            pending={
                r.stage.name: self.bus.pending(r.stage.input_topic, r.stage.group)
                for r in self.runners
            },
        )
