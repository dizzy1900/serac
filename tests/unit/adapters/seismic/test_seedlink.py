"""SeedLink feed driven by a fake client whose `run` replays fixture traces (offline)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from obspy import Trace, read

from serac.adapters.seismic.seedlink import (
    UNVERIFIED_NOTE,
    SeedLinkClientLike,
    SeedLinkFeed,
    SeedLinkFeedError,
)
from serac.domain.seismic import SeismicTrace, Sncl, TraceEncoding, TraceSource
from serac.ports.clock import VirtualClock

KKN = Sncl(network="NK", station="KKN", location="", channel="BHZ")
EVN = Sncl(network="IO", station="EVN", location="", channel="BHZ")


def _records(fixtures_dir: Path) -> list[Trace]:
    """Fixture traces cut into 512-sample pieces, standing in for SeedLink packets."""
    out: list[Trace] = []
    for name in ("NK.KKN..BHZ.mseed", "IO.EVN..BHZ.mseed"):
        trace = read(str(fixtures_dir / "seismic" / "langtang-2026" / name))[0]
        for i in range(3):
            piece = trace.slice(
                trace.stats.starttime + i * 10,
                trace.stats.starttime + (i + 1) * 10 - trace.stats.delta,
            )
            out.append(piece)
    return out


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, server: str, traces: list[Trace], *, failures: list[int]) -> None:
        self.server = server
        self.traces = traces
        self.failures = failures  # shared counter across reconnects: [remaining failures]
        self.selected: list[tuple[str, str, str | None]] = []
        self.closed = False
        self.on_data: Callable[[Trace], None] = lambda _t: None
        FakeClient.instances.append(self)

    def select_stream(self, net: str, station: str, selector: str | None = None) -> None:
        self.selected.append((net, station, selector))

    def run(self) -> None:
        if self.failures[0] > 0:
            self.failures[0] -= 1
            raise OSError("connection reset")
        for trace in self.traces:
            self.on_data(trace)

    def terminate(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_instances() -> None:
    FakeClient.instances.clear()


def _factory(traces: list[Trace], *, fail_first: int = 0) -> Callable[[str], SeedLinkClientLike]:
    failures = [fail_first]

    def make(server: str) -> SeedLinkClientLike:
        return FakeClient(server, traces, failures=failures)

    return make


def test_describe_is_a_dry_run_that_flags_the_endpoint_unverified() -> None:
    feed = SeedLinkFeed("example.invalid:18000", client_factory=_factory([]))
    feed.subscribe([KKN, EVN])
    described = feed.describe().as_dict()
    assert described["server"] == "example.invalid:18000"
    assert described["streams"] == ["NK.KKN..BHZ", "IO.EVN..BHZ"]
    assert described["verified_live"] is False
    assert UNVERIFIED_NOTE in described["notes"]
    assert FakeClient.instances == []  # nothing connected


def test_server_defaults_to_settings() -> None:
    feed = SeedLinkFeed(client_factory=_factory([]))
    assert feed.server == "geofon.gfz.de:18000"


def test_run_delivers_chunks_with_per_sncl_sequence(fixtures_dir: Path) -> None:
    traces = _records(fixtures_dir)
    feed = SeedLinkFeed("example.invalid:18000", client_factory=_factory(traces))
    feed.subscribe([KKN, EVN])
    got: list[SeismicTrace] = []
    delivered = feed.run(got.append, max_chunks=None)
    assert delivered == 6 == len(got)
    client = FakeClient.instances[0]
    assert client.selected == [("NK", "KKN", "BHZ"), ("IO", "EVN", "BHZ")]
    assert [c.sequence for c in got if c.sncl == KKN] == [0, 1, 2]
    assert [c.sequence for c in got if c.sncl == EVN] == [0, 1, 2]
    for chunk in got:
        assert chunk.encoding == TraceEncoding.miniseed
        assert chunk.provenance.source == TraceSource.seedlink
        assert chunk.provenance.server == "example.invalid:18000"
        assert chunk.provenance.notes == UNVERIFIED_NOTE
        assert chunk.npts == 500 or chunk.npts == 200  # 10 s at 50 Hz / 20 Hz


def test_max_chunks_stops_the_stream_and_closes(fixtures_dir: Path) -> None:
    feed = SeedLinkFeed("x:1", client_factory=_factory(_records(fixtures_dir)))
    feed.subscribe([KKN])
    got: list[SeismicTrace] = []
    assert feed.run(got.append, max_chunks=2) == 2
    assert len(got) == 2
    assert FakeClient.instances[0].closed


def test_reconnects_then_gives_up(fixtures_dir: Path) -> None:
    clock = VirtualClock()
    feed = SeedLinkFeed(
        "x:1",
        client_factory=_factory(_records(fixtures_dir), fail_first=2),
        clock=clock,
        max_reconnects=3,
        reconnect_delay_s=5.0,
    )
    feed.subscribe([KKN])
    got: list[SeismicTrace] = []
    assert feed.run(got.append, max_chunks=1) == 1
    assert feed.reconnects == 2
    assert clock.sleeps == [5.0, 5.0]

    feed2 = SeedLinkFeed(
        "x:1", client_factory=_factory([], fail_first=10), clock=clock, max_reconnects=1
    )
    feed2.subscribe([KKN])
    with pytest.raises(SeedLinkFeedError, match="gave up after 1 reconnects"):
        feed2.run(got.append, max_chunks=None)


def test_run_requires_subscription() -> None:
    feed = SeedLinkFeed("x:1", client_factory=_factory([]))
    with pytest.raises(SeedLinkFeedError, match="subscribe"):
        feed.run(lambda _c: None, max_chunks=None)
    with pytest.raises(SeedLinkFeedError):
        feed.subscribe([])
