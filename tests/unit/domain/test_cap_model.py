from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from serac.domain.cap import CONTRACTS, CAPArea, CAPInfo, CAPKeyValue, CAPMessage

SENT = datetime(2026, 8, 26, 2, 55, tzinfo=UTC)


def info(**overrides: object) -> CAPInfo:
    fields: dict[str, object] = {
        "category": ["Geo"],
        "event": "Long-period seismic signal (stub)",
        "urgency": "Unknown",
        "severity": "Unknown",
        "certainty": "Unknown",
    }
    fields.update(overrides)
    return CAPInfo.model_validate(fields)


def message(**overrides: object) -> CAPMessage:
    fields: dict[str, object] = {
        "identifier": "serac-stub-0001",
        "sender": "serac-stub@serac.invalid",
        "sent": SENT,
        "status": "Test",
        "msg_type": "Alert",
        "scope": "Private",
        "addresses": "serac-dev",
        "info": [info()],
    }
    fields.update(overrides)
    return CAPMessage.model_validate(fields)


def test_stub_shape_is_valid() -> None:
    msg = message()
    assert msg.info[0].area == []
    assert msg.info[0].language == "en-US"
    assert msg.xml is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "Live"),
        ("msg_type", "Notice"),
        ("scope", "Everyone"),
    ],
)
def test_alert_enums_match_cap_1_2(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        message(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", ["Volcano"]),
        ("urgency", "Soon"),
        ("severity", "Catastrophic"),
        ("certainty", "Certain"),
        ("response_type", ["Run"]),
    ],
)
def test_info_enums_match_cap_1_2(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        info(**{field: value})


def test_info_needs_at_least_one_category() -> None:
    with pytest.raises(ValidationError):
        info(category=[])


def test_private_scope_requires_addresses() -> None:
    with pytest.raises(ValidationError, match="addresses"):
        message(addresses=None)


def test_restricted_scope_requires_restriction() -> None:
    with pytest.raises(ValidationError, match="restriction"):
        message(scope="Restricted", addresses=None)
    message(scope="Restricted", addresses=None, restriction="internal test")


def test_public_scope_needs_nothing_extra() -> None:
    message(scope="Public", addresses=None)


@pytest.mark.parametrize("msg_type", ["Update", "Cancel", "Ack", "Error"])
def test_non_alert_types_require_references(msg_type: str) -> None:
    with pytest.raises(ValidationError, match="references"):
        message(msg_type=msg_type)
    message(
        msg_type=msg_type,
        references="serac-stub@serac.invalid,serac-stub-0000,2026-08-26T02:50:00+00:00",
    )


@pytest.mark.parametrize("bad", ["has space", "has,comma", "a<b", "a&b", ""])
def test_identifier_and_sender_tokens(bad: str) -> None:
    with pytest.raises(ValidationError):
        message(identifier=bad)
    with pytest.raises(ValidationError):
        message(sender=bad)


def test_area_ceiling_requires_altitude() -> None:
    with pytest.raises(ValidationError, match="altitude"):
        CAPArea(area_desc="x", ceiling=100.0)
    area = CAPArea(area_desc="x", altitude=0.0, ceiling=100.0, circle=["28.27,85.51 5"])
    assert area.circle == ["28.27,85.51 5"]


def test_headline_length_cap() -> None:
    with pytest.raises(ValidationError):
        info(headline="x" * 161)


def test_json_round_trip_with_parameters() -> None:
    msg = message(
        info=[
            info(
                parameter=[CAPKeyValue(value_name="serac:is_stub", value="true")],
                event_code=[CAPKeyValue(value_name="serac", value="LP-STUB")],
            )
        ],
        code=["serac-stub"],
    )
    assert CAPMessage.model_validate_json(msg.model_dump_json()) == msg


def test_contract_registry() -> None:
    assert {"cap-message": CAPMessage} == CONTRACTS
