"""The committed ESEC fixture parses to the catalogue the model card describes."""

from __future__ import annotations

import collections

import pytest
from tests.conftest import REPO_ROOT

from serac.adapters.seismic.esec import (
    EsecError,
    EsecSubType,
    load_esec_fixture,
    parse_esec_xml,
)

EXPECTED_EVENTS = 319
EXPECTED_SUBTYPES = {
    EsecSubType.rock_ice_debris_avalanche: 172,
    EsecSubType.rock_debris_fall: 65,
    EsecSubType.lahar_debris_flow: 52,
    EsecSubType.snow_avalanche: 26,
    EsecSubType.mine_collapse: 3,
    EsecSubType.flank_collapse: 1,
}


def test_fixture_parses_to_the_documented_catalogue() -> None:
    events = load_esec_fixture(REPO_ROOT)
    assert len(events) == EXPECTED_EVENTS
    counts = collections.Counter(e.sub_type for e in events)
    assert dict(counts) == EXPECTED_SUBTYPES


def test_every_event_has_a_time_and_a_location() -> None:
    for event in load_esec_fixture(REPO_ROOT):
        assert event.start_utc.tzinfo is not None
        assert -90 <= event.latitude <= 90
        assert -180 <= event.longitude <= 180


def test_crown_is_preferred_when_esec_gives_one() -> None:
    events = load_esec_fixture(REPO_ROOT)
    crowned = [e for e in events if e.crown_latitude is not None]
    assert len(crowned) == 161
    for event in crowned:
        assert event.location == (event.crown_latitude, event.crown_longitude)
        assert event.location_basis == "esec_crown"
    for event in events:
        if event.crown_latitude is None:
            assert event.location == (event.latitude, event.longitude)
            assert event.location_basis == "esec_nominal"


def test_units_are_null_where_the_document_states_none() -> None:
    """ESEC names a unit only in tags like `MaxdisthfKm`. H, L, Volume and Mass name none."""
    for event in load_esec_fixture(REPO_ROOT):
        for measurement in (
            event.fall_height,
            event.runout_length,
            event.volume,
            event.mass,
            event.area_total,
        ):
            if measurement is not None:
                assert measurement.unit is None, (
                    f"{measurement.source_tag} must not carry an invented unit"
                )


def test_a_truncated_document_is_refused() -> None:
    """A short download must fail loudly rather than silently shrink the positive set."""
    with pytest.raises(EsecError, match="truncated"):
        parse_esec_xml(
            '<Results count="3"><EsecEvents id="1"><EventId>1</EventId>'
            "<SubType>Snow avalanches</SubType><Type>x</Type>"
            "<Starttime>2020-01-01T00:00:00</Starttime>"
            "<Latitude>1.0</Latitude><Longitude>2.0</Longitude></EsecEvents></Results>"
        )


def test_the_html_escaped_response_is_refused_with_the_fix_in_the_message() -> None:
    with pytest.raises(EsecError, match="Accept: application/xml"):
        parse_esec_xml("<html><body><pre>&lt;Results&gt;</pre></body></html>")
