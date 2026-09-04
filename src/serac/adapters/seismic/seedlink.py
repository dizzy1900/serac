"""SeedLink live feed (`WaveformFeed`) over ObsPy's `EasySeedLinkClient`.

Each SeedLink packet arrives as one ObsPy trace (one 512-byte MiniSEED record); it is
re-encoded through `obspy_codec.trace_to_chunk` so a live record and a replay chunk look the
same downstream. Sequence numbers count per SNCL from zero for the lifetime of the feed.

The server hostname comes from settings (`SERAC_SEEDLINK_SERVER`, default
`geofon.gfz.de:18000`). **That endpoint is configuration, not a verified fact**: nothing in
this repository has connected to it (RELEASE_STATUS.md Known gaps 58). `describe()` is the
dry run and says so. The reconnect loop retries a bounded number of times with a `Clock`
sleep so tests can drive it without waiting.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from obspy import Trace
from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

from serac.adapters.seismic.obspy_codec import trace_to_chunk
from serac.domain.seismic import SeismicTrace, Sncl, TraceProvenance, TraceSource
from serac.errors import SeracError
from serac.ports.clock import Clock, WallClock
from serac.ports.seismic import WaveformFeed
from serac.settings import get_settings

ADAPTER_NAME = "SeedLinkFeed"
ADAPTER_VERSION = "0.1.0"
UNVERIFIED_NOTE = (
    "SeedLink server hostname is configuration (SERAC_SEEDLINK_SERVER); it has not been "
    "verified live in this repository. Verify with `make smoke-online`."
)


class SeedLinkFeedError(SeracError):
    """The feed could not subscribe or stream."""


class SeedLinkClientLike(Protocol):
    """What the feed needs from a client: ObsPy's `EasySeedLinkClient` or a test fake."""

    on_data: Callable[[Trace], None]

    def select_stream(self, net: str, station: str, selector: str | None = None) -> None: ...

    def run(self) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _ObsPyClient(EasySeedLinkClient):  # type: ignore[misc]
    """`EasySeedLinkClient` whose `on_data` is an instance attribute the feed replaces."""

    def __init__(self, server: str) -> None:
        super().__init__(server, autoconnect=True)
        self.on_data: Callable[[Trace], None] = lambda _trace: None

    def terminate(self) -> None:
        self.conn.terminate()


def obspy_client_factory(server: str) -> SeedLinkClientLike:
    """Default factory: connects immediately (network)."""
    return _ObsPyClient(server)


class _StopStreamingError(Exception):
    """Raised inside `on_data` to leave `client.run()` once `max_chunks` is reached."""


@dataclass
class FeedDescription:
    """Dry-run description of what `run` would do."""

    server: str
    streams: list[str]
    verified_live: bool = False
    notes: list[str] = field(default_factory=lambda: [UNVERIFIED_NOTE])

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": ADAPTER_NAME,
            "server": self.server,
            "streams": list(self.streams),
            "verified_live": self.verified_live,
            "notes": list(self.notes),
        }


class SeedLinkFeed(WaveformFeed):
    """Live chunks from a SeedLink server, delivered to a callback."""

    def __init__(
        self,
        server: str | None = None,
        *,
        client_factory: Callable[[str], SeedLinkClientLike] = obspy_client_factory,
        clock: Clock | None = None,
        max_reconnects: int = 3,
        reconnect_delay_s: float = 5.0,
    ) -> None:
        self.server = server or get_settings().serac_seedlink_server
        self._factory = client_factory
        self._clock = clock or WallClock()
        self.max_reconnects = max_reconnects
        self.reconnect_delay_s = reconnect_delay_s
        self._sncls: list[Sncl] = []
        self._client: SeedLinkClientLike | None = None
        self._sequence: dict[str, int] = {}
        self._delivered = 0
        self.reconnects = 0

    def subscribe(self, sncls: Sequence[Sncl]) -> None:
        if not sncls:
            raise SeedLinkFeedError("subscribe needs at least one SNCL")
        self._sncls = list(sncls)

    def describe(self) -> FeedDescription:
        """What `run` would subscribe to; touches no network."""
        return FeedDescription(server=self.server, streams=[s.key for s in self._sncls])

    def _provenance(self) -> TraceProvenance:
        return TraceProvenance(
            source=TraceSource.seedlink,
            server=self.server,
            retrieved_at=datetime.now(tz=UTC),
            notes=UNVERIFIED_NOTE,
        )

    def _chunk(self, trace: Trace) -> SeismicTrace:
        stats = trace.stats
        key = f"{stats.network}.{stats.station}.{stats.location}.{stats.channel}"
        sequence = self._sequence.get(key, 0)
        self._sequence[key] = sequence + 1
        return trace_to_chunk(trace, provenance=self._provenance(), sequence=sequence)

    def _open(self) -> SeedLinkClientLike:
        client = self._factory(self.server)
        for sncl in self._sncls:
            client.select_stream(sncl.network, sncl.station, sncl.channel)
        self._client = client
        return client

    def run(self, on_chunk: Callable[[SeismicTrace], None], *, max_chunks: int | None) -> int:
        if not self._sncls:
            raise SeedLinkFeedError("call subscribe() before run()")
        if max_chunks is not None and max_chunks < 1:
            raise SeedLinkFeedError("max_chunks must be positive or None")
        start_count = self._delivered

        def handle(trace: Trace) -> None:
            on_chunk(self._chunk(trace))
            self._delivered += 1
            if max_chunks is not None and self._delivered - start_count >= max_chunks:
                raise _StopStreamingError

        attempts = 0
        while True:
            try:
                client = self._open()
                client.on_data = handle
                client.run()
                return self._delivered - start_count  # server ended the stream
            except _StopStreamingError:
                self.close()
                return self._delivered - start_count
            except Exception as exc:
                self.close()
                attempts += 1
                if attempts > self.max_reconnects:
                    raise SeedLinkFeedError(
                        f"SeedLink {self.server}: gave up after {attempts - 1} reconnects: {exc}"
                    ) from exc
                self.reconnects += 1
                self._clock.sleep(self.reconnect_delay_s)

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        # closing a half-open connection must not mask the real error
        with contextlib.suppress(Exception):
            client.close()
