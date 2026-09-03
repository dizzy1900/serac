"""Speed 1.0 pacing proven with a VirtualClock: the sleep schedule follows stream time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from serac.pipelines.replay import ReplayConfig, ReplayError, parse_speed, run_replay
from serac.ports.clock import VirtualClock
from serac.streaming.replay_source import SYNTHETIC_EVENT_ID, SyntheticReplaySource

START = datetime(2030, 1, 1, tzinfo=UTC)


def test_parse_speed() -> None:
    assert parse_speed("max") == "max"
    assert parse_speed("1.0") == 1.0
    assert parse_speed("2") == 2.0
    with pytest.raises(ReplayError):
        parse_speed("0")
    with pytest.raises(ReplayError):
        parse_speed("fast")


def test_speed_one_sleeps_one_chunk_interval_per_chunk(repo_root: Path, tmp_path: Path) -> None:
    clock = VirtualClock(START)
    source = SyntheticReplaySource(n_chunks=8)
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID,
        speed=1.0,
        chunk_seconds=5.0,
        repo_root=repo_root,
        report_dir=tmp_path,
    )
    report = run_replay(config, clock=clock, source=source)
    assert report.status == "completed"
    # First chunk: no wait. Each later chunk waits until (started + k * 5 s); processing takes
    # zero virtual time, so every recorded sleep is exactly 5 s.
    assert clock.sleeps == pytest.approx([0.0] + [5.0] * 7)
    assert clock.total_slept_s == pytest.approx(35.0)
    assert report.started_at_utc == START
    assert report.finished_at_utc == START + timedelta(seconds=35)
    assert report.wall_clock_latencies.valid is True
    assert report.wall_clock_latencies.total_run_s == pytest.approx(35.0)
    assert not any("wall-clock latencies are not comparable" in c for c in report.caveats)


def test_speed_two_halves_the_waits(repo_root: Path, tmp_path: Path) -> None:
    clock = VirtualClock(START)
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID,
        speed=2.0,
        chunk_seconds=5.0,
        repo_root=repo_root,
        report_dir=tmp_path,
    )
    report = run_replay(config, clock=clock, source=SyntheticReplaySource(n_chunks=4))
    assert clock.sleeps == pytest.approx([0.0, 2.5, 2.5, 2.5])
    assert report.wall_clock_latencies.valid is False  # only speed 1.0 is comparable to live


def test_speed_max_never_sleeps(repo_root: Path, tmp_path: Path) -> None:
    clock = VirtualClock(START)
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID, speed="max", repo_root=repo_root, report_dir=tmp_path
    )
    run_replay(config, clock=clock, source=SyntheticReplaySource(n_chunks=4))
    assert clock.sleeps == []


def test_late_processing_shortens_the_next_wait(repo_root: Path, tmp_path: Path) -> None:
    """If a stage burns virtual time, the pacer waits less (or not at all) for the next chunk."""

    class SlowClock(VirtualClock):
        def now(self) -> datetime:
            return super().now()

        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            self.advance(3.0)  # every drain 'takes' 3 s of virtual time

    clock = SlowClock(START)
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID, speed=1.0, repo_root=repo_root, report_dir=tmp_path
    )
    run_replay(config, clock=clock, source=SyntheticReplaySource(n_chunks=4))
    assert clock.sleeps == pytest.approx([0.0, 2.0, 2.0, 2.0])
