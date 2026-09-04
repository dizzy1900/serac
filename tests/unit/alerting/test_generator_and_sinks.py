"""The CAP generator's derivation rules, and the two alert sinks.

The rules are the point: a reviewer should be able to read these tests and know what makes a
message `Test` rather than `Actual`, and what makes an `area` appear.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from serac.adapters.alerting.file_sink import INDEX_FILENAME, FileAlertSink, safe_filename
from serac.adapters.alerting.http_sink import NOT_ENABLED_DETAIL, HttpAlertSink
from serac.alerting.example import check_forecast
from serac.alerting.generator import (
    CapGenerationError,
    area_for,
    build_alert,
    delivered_lead_time_min,
    severity_for,
    status_for,
    urgency_for,
)
from serac.domain.forecast import ConfidenceTier, ModelProvenance
from serac.ports.alert_sink import AlertSinkError
from serac.validation.cap import CapValidator


def _xsd() -> Path:
    return Path(__file__).resolve().parents[3] / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"


@pytest.fixture
def validator() -> CapValidator:
    return CapValidator(_xsd())


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (ConfidenceTier.unqualified, "Test"),
        (ConfidenceTier.low, "Exercise"),
        (ConfidenceTier.medium, "Exercise"),
        (ConfidenceTier.high, "Actual"),
    ],
)
def test_status_follows_the_confidence_tier(tier: ConfidenceTier, expected: str) -> None:
    forecast = check_forecast(provenance=ModelProvenance.surrogate, confidence_tier=tier)
    status, rule = status_for(forecast)
    assert status == expected
    assert str(tier) in rule


def test_a_stub_forecast_is_always_test_whatever_the_tier_would_give() -> None:
    """The brief's rule: a test tier must still produce status: Test."""
    forecast = check_forecast(
        provenance=ModelProvenance.stub, confidence_tier=ConfidenceTier.unqualified
    )
    status, rule = status_for(forecast)
    assert status == "Test"
    assert "forces status=Test" in rule


def test_an_actual_message_is_public_and_a_test_message_is_private(
    validator: CapValidator,
) -> None:
    test_build = build_alert(
        check_forecast(), sent=check_forecast().issued_utc, validator=validator
    )
    assert test_build.message.scope == "Private"
    assert test_build.message.addresses == "serac-operators"

    high = check_forecast(provenance=ModelProvenance.simulator, confidence_tier=ConfidenceTier.high)
    actual = build_alert(high, sent=high.issued_utc, validator=validator)
    assert actual.message.status == "Actual"
    assert actual.message.scope == "Public"
    assert actual.message.addresses is None


def test_severity_comes_from_the_largest_peak_stage_and_says_it_is_an_assumption() -> None:
    severity, rule = severity_for(check_forecast())
    assert severity == "Extreme"  # the check fixture's 95th-percentile stage is 6 m
    assert "ASSUMPTION" in rule


def test_severity_is_unknown_when_no_stage_was_modelled() -> None:
    forecast = check_forecast()
    stripped = forecast.model_copy(
        update={
            "transect_arrivals": [
                a.model_copy(update={"peak_stage_m": None}) for a in forecast.transect_arrivals
            ]
        }
    )
    severity, rule = severity_for(stripped)
    assert severity == "Unknown"
    assert "rather than guessed" in rule


def test_urgency_is_unknown_when_no_transect_is_reached() -> None:
    forecast = check_forecast().model_copy(update={"transect_arrivals": []})
    urgency, rule = urgency_for(forecast)
    assert urgency == "Unknown"
    assert "reaches no transect" in rule


def test_area_uses_the_footprint_and_orders_vertices_lat_lon() -> None:
    area, rule = area_for(check_forecast())
    assert area is not None
    assert area.polygon and area.polygon[0].startswith("0.000000,0.000000")
    assert {g.value_name for g in area.geocode} == {"serac:aoi_id", "serac:transect_id"}
    assert "footprint polygon" in rule


def test_no_area_is_invented_when_there_is_neither_footprint_nor_arrival() -> None:
    forecast = check_forecast(with_footprint=False).model_copy(update={"transect_arrivals": []})
    area, rule = area_for(forecast)
    assert area is None
    assert "no area" in rule


def test_a_footprintless_forecast_still_describes_the_transects_it_reaches() -> None:
    area, rule = area_for(check_forecast(with_footprint=False))
    assert area is not None
    assert area.polygon == []
    assert "no footprint polygon" in rule


def test_delivered_lead_time_can_be_negative() -> None:
    forecast = check_forecast()
    arrival = forecast.transect_arrivals[0].arrival_time_min
    late = delivered_lead_time_min(
        forecast, arrival, forecast.origin_time_utc + timedelta(minutes=100)
    )
    assert late.high < 0
    assert "negative means the flow arrives before the alert" in (late.notes or "")


def test_the_message_carries_the_rules_and_every_transect_eta(validator: CapValidator) -> None:
    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, validator=validator)
    names = {p.value_name for p in build.message.info[0].parameter}
    assert "serac:status_rule" in names
    assert "serac:severity_rule" in names
    assert "serac:eta_min:fictional-transect-a" in names
    assert "serac:peak_stage_m:fictional-transect-a" in names
    assert "serac:delivered_lead_time_min:fictional-transect-b" in names
    assert "serac:damming_probability" in names
    assert "serac:secondary_surge_min:fictional-transect-b" in names
    assert validator.errors(build.message.xml or "") == []


def test_a_naive_sent_time_is_refused(validator: CapValidator) -> None:
    from datetime import datetime

    with pytest.raises(CapGenerationError, match="timezone-aware"):
        build_alert(check_forecast(), sent=datetime(2000, 1, 1), validator=validator)


# -- sinks ---------------------------------------------------------------------------------------


def test_file_sink_writes_the_xml_and_an_index(tmp_path: Path, validator: CapValidator) -> None:
    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, validator=validator)
    sink = FileAlertSink(tmp_path)
    delivery = sink.deliver(build.message)
    assert delivery.delivered
    assert delivery.target and Path(delivery.target).exists()
    rows = [
        json.loads(line)
        for line in (tmp_path / INDEX_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["identifier"] == build.message.identifier
    assert rows[0]["signed"] is False


def test_file_sink_reports_a_message_with_no_xml_rather_than_raising(tmp_path: Path) -> None:
    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, xsd_path=_xsd())
    delivery = FileAlertSink(tmp_path).deliver(build.message.model_copy(update={"xml": None}))
    assert not delivery.delivered
    assert "no rendered XML" in delivery.detail


def test_safe_filename_refuses_an_identifier_with_no_safe_form() -> None:
    with pytest.raises(AlertSinkError):
        safe_filename("///")


def test_http_sink_has_no_default_endpoint() -> None:
    with pytest.raises(AlertSinkError, match="no default destination"):
        HttpAlertSink("")
    with pytest.raises(AlertSinkError):
        HttpAlertSink("not-a-url")


def test_http_sink_sends_nothing_unless_enabled(validator: CapValidator) -> None:
    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, validator=validator)

    class ExplodingSession:
        def post(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("the disabled HTTP sink must not post")

    sink = HttpAlertSink("https://example.invalid/cap", session=ExplodingSession())
    delivery = sink.deliver(build.message)
    assert not delivery.delivered
    assert delivery.detail == NOT_ENABLED_DETAIL
    assert sink.posted == 0


def test_http_sink_posts_when_enabled_and_reports_transport_failures(
    validator: CapValidator,
) -> None:
    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, validator=validator)

    class Response:
        status_code = 202

    class OkSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def post(self, url: str, **kwargs: Any) -> Response:
            self.calls.append({"url": url, **kwargs})
            return Response()

    session = OkSession()
    sink = HttpAlertSink("https://example.invalid/cap", enabled=True, session=session)
    delivery = sink.deliver(build.message)
    assert delivery.delivered and delivery.detail == "HTTP 202"
    assert session.calls[0]["headers"]["Content-Type"] == "application/cap+xml"

    class FailingSession:
        def post(self, *args: Any, **kwargs: Any) -> Response:
            raise TimeoutError("no route")

    failed = HttpAlertSink(
        "https://example.invalid/cap", enabled=True, session=FailingSession()
    ).deliver(build.message)
    assert not failed.delivered
    assert "TimeoutError" in failed.detail
