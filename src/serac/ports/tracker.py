"""Port for experiment tracking.

serac must stay usable with no external account, so the local filesystem adapter is the
default and the Weights & Biases adapter is opt-in. Tests always use the local one, which
writes plain JSON under `reports/experiments/` and is therefore diffable and offline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TRACKER_PORT_VERSION = "0.1.0"

Scalar = float | int | str | bool


class RunRecord(BaseModel):
    """One experiment run: its config, its metrics over time, and its outcome."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    project: str = Field(min_length=1)
    name: str = Field(min_length=1)
    started_at_utc: AwareDatetime
    finished_at_utc: AwareDatetime | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    git_sha: str | None = None
    serac_version: str | None = None
    status: str = "running"


class Tracker(ABC):
    """Records a run's config and metrics somewhere durable."""

    @abstractmethod
    def start(self, *, project: str, name: str, config: dict[str, Any], tags: list[str]) -> str:
        """Begin a run and return its id."""

    @abstractmethod
    def log(self, metrics: dict[str, Scalar], *, step: int | None = None) -> None:
        """Record one row of metrics."""

    @abstractmethod
    def summarise(self, summary: dict[str, Any]) -> None:
        """Record the run's final numbers."""

    @abstractmethod
    def finish(self, status: str = "finished") -> None:
        """Close the run."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.finish("failed" if exc_type is not None else "finished")
