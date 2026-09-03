"""Online smoke tests for the seismic data sources (run via `make smoke-online`).

These prove the services still answer the fixture requests; they do not compare bytes, because
the fixture script already refuses to overwrite a differing download.
"""

from __future__ import annotations

import warnings

import httpx
import pytest
from tests.conftest import require_network

pytestmark = pytest.mark.online


def test_earthscope_dataselect_serves_kkn() -> None:
    require_network("service.earthscope.org")
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = Client("EARTHSCOPE", timeout=60)
    assert client.base_url == "https://service.earthscope.org"
    t0 = UTCDateTime(2021, 2, 7, 4, 51)
    stream = client.get_waveforms("NK", "KKN", "", "BHZ", t0, t0 + 30)
    assert len(stream) >= 1
    assert stream[0].stats.npts > 0
    assert stream[0].stats.sampling_rate == 50.0


def test_earthscope_station_serves_channel_level() -> None:
    require_network("service.earthscope.org")
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = Client("EARTHSCOPE", timeout=60)
    t0 = UTCDateTime(2026, 8, 26, 2, 50)
    inventory = client.get_stations(
        network="IO", station="EVN", channel="BHZ", level="channel", starttime=t0, endtime=t0 + 60
    )
    assert inventory.get_contents()["channels"]


def test_comcat_serves_the_langtang_landslide_event() -> None:
    require_network("earthquake.usgs.gov")
    response = httpx.get(
        "https://earthquake.usgs.gov/fdsnws/event/1/query",
        params={"eventid": "us7000tbwb", "format": "geojson"},
        timeout=60,
        follow_redirects=True,
    )
    response.raise_for_status()
    doc = response.json()
    assert doc["id"] == "us7000tbwb"
    assert doc["properties"]["type"] == "landslide"


def test_cap_schema_still_published() -> None:
    require_network("docs.oasis-open.org")
    response = httpx.head(
        "https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.xsd",
        timeout=60,
        follow_redirects=True,
    )
    assert response.status_code == 200
