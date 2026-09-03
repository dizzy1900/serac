"""SeedLink ingestor with a fake feed: every chunk lands on serac.waveforms."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from serac.adapters.bus.in_memory import InMemoryBus
from serac.domain import topics
from serac.domain.seismic import SeismicTrace, Sncl
from serac.ports.seismic import WaveformFeed
from serac.streaming.seedlink_ingestor import SeedLinkIngestor
from serac.streaming.synthetic import synthetic_chunks

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeFeed(WaveformFeed):
    def __init__(self, chunks: list[SeismicTrace]) -> None:
        self.chunks = chunks
        self.subscribed: list[Sncl] = []
        self.closed = False

    def subscribe(self, sncls: Sequence[Sncl]) -> None:
        self.subscribed = list(sncls)

    def run(self, on_chunk: Callable[[SeismicTrace], None], *, max_chunks: int | None) -> int:
        n = 0
        for chunk in self.chunks:
            if max_chunks is not None and n >= max_chunks:
                break
            on_chunk(chunk)
            n += 1
        return n

    def close(self) -> None:
        self.closed = True


def test_ingestor_publishes_every_chunk_and_closes_the_feed() -> None:
    chunks = list(synthetic_chunks(start_utc=T0, n_chunks=4))
    feed = FakeFeed(chunks)
    bus = InMemoryBus()
    summary = SeedLinkIngestor(feed, bus).run([chunks[0].sncl], max_chunks=3)
    assert summary.chunks_published == 3
    assert summary.sncls == ("XX.SYNTH..BHZ",)
    assert feed.subscribed == [chunks[0].sncl]
    assert feed.closed
    log = bus.log(topics.WAVEFORMS)
    assert len(log) == 3
    assert all(e.producer == "seedlink-ingestor" and e.schema_name == "seismic-trace" for e in log)
    assert [e.stream_time_utc for e in log] == [c.start_time_utc for c in chunks[:3]]
