"""Real CAP stage: Test/Private/no-area on detections; build_alert on forecasts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from lxml import etree
from pydantic import BaseModel

from serac.adapters.bus.in_memory import InMemoryBus
from serac.adapters.cap.cap12 import CAP_NS, render
from serac.alerting.example import check_forecast
from serac.alerting.generator import DEFAULT_SENDER, STATUS_BY_TIER
from serac.alerting.keys import SIGNING_KEY_ENV, generate_keypair, write_private_key
from serac.alerting.signing import is_signed, verify_cap_signature
from serac.domain import topics
from serac.domain.cap import CAPMessage
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate, DetectionLocation
from serac.domain.envelope import Envelope
from serac.domain.forecast import FORECAST_CONTRACT_VERSION, ConfidenceTier, ModelProvenance
from serac.domain.seismic import Sncl
from serac.ports.bus import Received
from serac.ports.clock import VirtualClock
from serac.streaming.cap_stage import (
    DETECTION_INSTRUCTION,
    CapStage,
    CapStageError,
    build_cap_stage,
    cap_message_for_detection,
    parse_cap_kind,
)
from serac.streaming.cap_stub import CapStub
from serac.streaming.stage import StageRunner
from serac.validation.cap import CapValidator

T0 = datetime(2026, 8, 26, 2, 53, tzinfo=UTC)


def detection(*, with_location: bool = False) -> DetectionCandidate:
    location = None
    if with_location:
        location = DetectionLocation(
            latitude=28.21,
            longitude=85.56,
            method="gsf_grid_search",
            grid_spacing_km=5.0,
            variance_reduction=0.4,
            azimuthal_gap_deg=80.0,
        )
    return DetectionCandidate(
        detection_id="lp-sp-ratio-stub/NK.KKN..BHZ/2026-08-26T02:54:59+00:00",
        sncl=Sncl(network="NK", station="KKN", location="", channel="BHZ"),
        detector="lp-sp-ratio-stub",
        detector_version="0.1.0",
        window_start_utc=T0,
        window_end_utc=T0.replace(minute=54, second=59),
        detected_at_stream_utc=T0.replace(minute=54, second=59),
        score=12.5,
        threshold=10.0,
        source_location=location,
        is_stub=not with_location,
        input_trace_ids=["a"],
    )


@pytest.fixture
def xsd(repo_root: Path) -> Path:
    return repo_root / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"


def test_parse_cap_kind() -> None:
    assert parse_cap_kind("xsd") == "xsd"
    assert parse_cap_kind(" STUB ") == "stub"
    with pytest.raises(ValueError, match="xsd"):
        parse_cap_kind("live")


def test_build_cap_stage_stub_keeps_cap_stub(xsd: Path) -> None:
    stage = build_cap_stage("stub", xsd_path=xsd)
    assert isinstance(stage, CapStub)
    assert stage.name == "cap-stub"


def test_build_cap_stage_default_is_the_xsd_generator(xsd: Path) -> None:
    stage = build_cap_stage("xsd", xsd_path=xsd, load_key_from_env=False)
    assert isinstance(stage, CapStage)
    assert stage.name == "cap"


def test_detection_path_is_test_private_unknown_and_has_no_area(xsd: Path) -> None:
    clock = VirtualClock(datetime(2026, 9, 1, tzinfo=UTC))
    stage = CapStage(xsd_path=xsd, clock=clock, load_key_from_env=False)
    message = stage.build(detection())
    assert message.status == "Test"
    assert message.scope == "Private"
    assert message.sender == DEFAULT_SENDER
    assert message.addresses == "serac-operators"
    assert message.source == "serac.streaming.cap_stage"
    info = message.info[0]
    assert (info.urgency, info.severity, info.certainty) == ("Unknown", "Unknown", "Unknown")
    assert info.area == []
    assert info.instruction == DETECTION_INSTRUCTION
    assert "research output" in (info.description or "")
    assert "not an operational alert" in (info.description or "")
    assert message.xml is not None
    root = etree.fromstring(message.xml.encode())
    assert root.find(f"{{{CAP_NS}}}info/{{{CAP_NS}}}area") is None
    assert root.findtext(f"{{{CAP_NS}}}status") == "Test"
    params = {
        p.findtext(f"{{{CAP_NS}}}valueName"): p.findtext(f"{{{CAP_NS}}}value")
        for p in root.iter(f"{{{CAP_NS}}}parameter")
    }
    assert params["serac:is_stub"] == "true"
    assert params["serac:source_location"] == "null"
    assert "STATUS_BY_TIER is not applied" in (params["serac:status_rule"] or "")
    assert CapValidator(xsd).is_valid(message.xml)
    assert not is_signed(message.xml)


def test_a_located_detection_still_has_no_area(xsd: Path) -> None:
    """A gSF point is a parameter, not a polygon. No geometry is invented."""
    message = CapStage(xsd_path=xsd, load_key_from_env=False).build(detection(with_location=True))
    assert message.info[0].area == []
    params = {p.value_name: p.value for p in message.info[0].parameter}
    assert params["serac:source_location"].startswith("28.210000,85.560000")
    assert "polygon" in params["serac:area_absent"]
    assert CapValidator(xsd).is_valid(message.xml or "")


def test_status_by_tier_is_unchanged() -> None:
    """High/Actual must remain the mapping; this change does not relax it."""
    assert STATUS_BY_TIER[ConfidenceTier.unqualified] == "Test"
    assert STATUS_BY_TIER[ConfidenceTier.low] == "Exercise"
    assert STATUS_BY_TIER[ConfidenceTier.medium] == "Exercise"
    assert STATUS_BY_TIER[ConfidenceTier.high] == "Actual"


def test_a_forecast_payload_uses_build_alert_and_status_by_tier(xsd: Path) -> None:
    clock = VirtualClock(T0)
    stage = CapStage(xsd_path=xsd, clock=clock, load_key_from_env=False)
    stub_forecast = check_forecast()
    stub_message = stage.build(stub_forecast)
    assert stub_message.status == "Test"
    assert stub_message.source == "serac.alerting.generator"

    high = check_forecast(provenance=ModelProvenance.simulator, confidence_tier=ConfidenceTier.high)
    actual = stage.build(high)
    assert actual.status == "Actual"
    assert actual.scope == "Public"
    assert actual.info[0].area  # fictional check fixture has a footprint; not invented here
    assert CapValidator(xsd).is_valid(actual.xml or "")


def test_naive_sent_is_refused(xsd: Path) -> None:
    with pytest.raises(CapStageError, match="timezone-aware"):
        cap_message_for_detection(detection(), sent=datetime(2026, 8, 26, 2, 53))


def test_refuses_to_publish_when_validation_fails(xsd: Path) -> None:
    class Rejecting(CapValidator):
        def errors(self, xml: bytes | str) -> list[str]:
            return ["forced failure"]

    stage = CapStage(validator=Rejecting(xsd), clock=VirtualClock(T0), load_key_from_env=False)
    with pytest.raises(CapStageError, match="refusing to publish"):
        stage.build(detection())
    assert stage.rendered == 0


def test_stage_publishes_on_alerts_with_causation(xsd: Path) -> None:
    bus = InMemoryBus()
    stage = CapStage(xsd_path=xsd, clock=VirtualClock(T0), load_key_from_env=False)
    runner = StageRunner(bus, stage)
    det = detection()
    env: Envelope[BaseModel] = wrap(
        det,
        topic=topics.DETECTIONS,
        producer="t",
        stream_time_utc=det.detected_at_stream_utc,
        replay_run_id="r",
    )
    bus.publish(env)
    assert runner.step() == 1
    alerts = bus.log(topics.ALERTS)
    assert len(alerts) == 1
    out = alerts[0]
    assert out.producer == "cap"
    assert out.causation_id == env.message_id and out.replay_run_id == "r"
    payload = out.payload
    assert isinstance(payload, CAPMessage) and payload.xml is not None
    assert payload.status == "Test"


def test_forecast_on_the_stage_does_not_need_the_bus_codec(xsd: Path) -> None:
    """Forecasts are not a bus schema yet; process() still accepts the payload type."""
    forecast = check_forecast()
    envelope: Envelope[BaseModel] = Envelope(
        topic=topics.DETECTIONS,
        schema_name="cascade-forecast",
        schema_version=FORECAST_CONTRACT_VERSION,
        producer="t",
        stream_time_utc=forecast.issued_utc,
        payload=forecast,
    )
    outputs = CapStage(xsd_path=xsd, load_key_from_env=False).process(
        Received(message_id="1-0", topic=topics.DETECTIONS, envelope=envelope)
    )
    assert len(outputs) == 1
    assert isinstance(outputs[0].payload, CAPMessage)
    assert outputs[0].payload.status == "Test"


def test_stage_rejects_wrong_payload(xsd: Path) -> None:
    from serac.domain.force_history import ForceHistory

    env: Envelope[BaseModel] = wrap(
        ForceHistory(), topic=topics.DETECTIONS, producer="t", stream_time_utc=T0
    )
    with pytest.raises(CapStageError, match="DetectionCandidate or CascadeForecast"):
        CapStage(xsd_path=xsd, load_key_from_env=False).process(
            Received(message_id="1-0", topic=topics.DETECTIONS, envelope=env)
        )


def test_signing_is_optional_and_a_missing_key_does_not_fail_the_lane(
    xsd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SIGNING_KEY_ENV, str(tmp_path / "absent.pem"))
    stage = CapStage(xsd_path=xsd, clock=VirtualClock(T0), load_key_from_env=True)
    message = stage.build(detection())
    assert message.xml is not None
    assert not is_signed(message.xml)
    assert CapValidator(xsd).is_valid(message.xml)


def test_a_present_key_signs_and_the_result_still_validates(
    xsd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = generate_keypair()
    path = write_private_key(key, tmp_path / "cap.pem", allow_tracked=True)
    monkeypatch.setenv(SIGNING_KEY_ENV, str(path))
    stage = CapStage(xsd_path=xsd, clock=VirtualClock(T0), load_key_from_env=True)
    message = stage.build(detection())
    assert message.xml is not None
    assert is_signed(message.xml)
    assert verify_cap_signature(message.xml, key.public_key())
    assert CapValidator(xsd).is_valid(message.xml)
    assert stage.signed == 1


def test_render_order_follows_the_schema(xsd: Path) -> None:
    message = CapStage(xsd_path=xsd, clock=VirtualClock(T0), load_key_from_env=False).build(
        detection()
    )
    root = etree.fromstring(render(message))
    tags = [etree.QName(c).localname for c in root]
    assert tags[:5] == ["identifier", "sender", "sent", "status", "msgType"]
    assert tags[-1] == "info"
