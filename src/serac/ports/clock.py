"""Clock port so replay pacing can be tested without sleeping."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    """Source of wall-clock time and the ability to wait."""

    @abstractmethod
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """Wait `seconds`; negative or zero returns immediately."""

    def sleep_until(self, when: datetime) -> None:
        """Wait until `when` (aware datetime); returns immediately if already past."""
        self.sleep((when - self.now()).total_seconds())


class WallClock(Clock):
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock(Clock):
    """A clock that only advances when told to; every sleep is recorded.

    `sleep` advances `now()` by the requested amount instantly, so a replay paced at speed 1.0
    can be tested in milliseconds while asserting the exact waits it would have made.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2000, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("VirtualClock start must be timezone-aware")
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep (simulates elapsed work)."""
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._now += timedelta(seconds=seconds)

    @property
    def total_slept_s(self) -> float:
        return sum(s for s in self.sleeps if s > 0)
