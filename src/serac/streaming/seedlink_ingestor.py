"""SeedLink ingestor: `WaveformFeed` chunks -> `Envelope[SeismicTrace]` on `serac.waveforms`.

The ingestor is a thin producer: it owns no buffer and no logic beyond wrapping each chunk in
an envelope whose `stream_time_utc` is the chunk start. It works with any `WaveformFeed`, so
tests drive it with a fake feed and never open a socket.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from serac.domain import topics
from serac.domain.codec import wrap
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace, Sncl
from serac.ports.bus import MessageBus
from serac.ports.seismic import WaveformFeed

PRODUCER = "seedlink-ingestor"


@dataclass(frozen=True)
class IngestSummary:
    chunks_published: int
    sncls: tuple[str, ...]


class SeedLinkIngestor:
    """Publishes every chunk a feed delivers."""

    def __init__(self, feed: WaveformFeed, bus: MessageBus, *, producer: str = PRODUCER) -> None:
        self.feed = feed
        self.bus = bus
        self.producer = producer
        self.published = 0
        self.last_message_id: str | None = None

    def publish(self, chunk: SeismicTrace) -> None:
        envelope: Envelope[BaseModel] = wrap(
            chunk,
            topic=topics.WAVEFORMS,
            producer=self.producer,
            stream_time_utc=chunk.start_time_utc,
        )
        self.last_message_id = self.bus.publish(envelope)
        self.published += 1

    def run(self, sncls: Sequence[Sncl], *, max_chunks: int | None = None) -> IngestSummary:
        """Subscribe and stream until `max_chunks` or the feed ends."""
        self.feed.subscribe(sncls)
        try:
            delivered = self.feed.run(self.publish, max_chunks=max_chunks)
        finally:
            self.feed.close()
        return IngestSummary(chunks_published=delivered, sncls=tuple(s.key for s in sncls))
