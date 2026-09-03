"""Online: does the configured SeedLink server answer and stream a packet? (allowed to skip)"""

from __future__ import annotations

import pytest
from tests.conftest import require_network

from serac.adapters.seismic.seedlink import SeedLinkFeed
from serac.domain.seismic import SeismicTrace, Sncl, TraceSource
from serac.settings import get_settings

pytestmark = pytest.mark.online


def test_seedlink_server_streams_one_record() -> None:
    server = get_settings().serac_seedlink_server
    host, _, port = server.partition(":")
    require_network(host, int(port or 18000))
    feed = SeedLinkFeed(server, max_reconnects=0)
    # GE.KBS is a long-running GEOFON broadband station; if the stream name changed this
    # test skips rather than asserting a fact about the server.
    feed.subscribe([Sncl(network="GE", station="KBS", location="00", channel="BHZ")])
    got: list[SeismicTrace] = []
    try:
        delivered = feed.run(got.append, max_chunks=1)
    except Exception as exc:
        pytest.skip(f"SeedLink {server} did not stream: {exc}")
    assert delivered == 1
    assert got[0].provenance.source == TraceSource.seedlink
    assert got[0].provenance.server == server
