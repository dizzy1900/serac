"""CAP stub: Test/Private/no-area messages, XSD-validated before publishing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from lxml import etree
from pydantic import BaseModel

from serac.adapters.bus.in_memory import InMemoryBus
from serac.adapters.cap.cap12 import CAP_NS, cap_datetime, render
from serac.domain import topics
from serac.domain.cap import CAPMessage
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.seismic import Sncl
from serac.ports.bus import Received
from serac.ports.clock import VirtualClock
from serac.streaming.cap_stub import STUB_ADDRESSES, STUB_SENDER, CapStub, CapStubError
from serac.streaming.stage import StageRunner
from serac.validation.cap import CapValidator

T0 = datetime(2026, 8, 26, 2, 53, tzinfo=UTC)


def detection() -> DetectionCandidate:
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
        input_trace_ids=["a"],
    )


@pytest.fixture
def xsd(repo_root: Path) -> Path:
    return repo_root / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"


def test_cap_datetime_has_no_fraction_and_numeric_offset() -> None:
    assert (
        cap_datetime(datetime(2026, 8, 26, 2, 53, 1, 999999, tzinfo=UTC))
        == "2026-08-26T02:53:01+00:00"
    )


def test_build_is_test_private_unknown_and_has_no_area(xsd: Path) -> None:
    clock = VirtualClock(datetime(2026, 9, 1, tzinfo=UTC))
    stub = CapStub(xsd_path=xsd, clock=clock)
    message = stub.build(detection())
    assert message.status == "Test"
    assert message.scope == "Private"
    assert message.sender == STUB_SENDER == "serac-stub@serac.invalid"
    assert message.addresses == STUB_ADDRESSES
    assert message.sent == clock.now()
    info = message.info[0]
    assert (info.urgency, info.severity, info.certainty) == ("Unknown", "Unknown", "Unknown")
    assert info.area == []
    assert message.xml is not None
    root = etree.fromstring(message.xml.encode())
    assert root.tag == f"{{{CAP_NS}}}alert"
    assert root.find(f"{{{CAP_NS}}}info/{{{CAP_NS}}}area") is None
    assert root.findtext(f"{{{CAP_NS}}}status") == "Test"
    params = {
        p.findtext(f"{{{CAP_NS}}}valueName"): p.findtext(f"{{{CAP_NS}}}value")
        for p in root.iter(f"{{{CAP_NS}}}parameter")
    }
    assert params["serac:is_stub"] == "true"
    assert params["serac:source_location"] == "null"
    assert params["serac:score"] == "12.5"
    assert CapValidator(xsd).is_valid(message.xml)


def test_render_order_follows_the_schema(xsd: Path) -> None:
    message = CapStub(xsd_path=xsd, clock=VirtualClock(T0)).build(detection())
    root = etree.fromstring(render(message))
    tags = [etree.QName(c).localname for c in root]
    assert tags[:5] == ["identifier", "sender", "sent", "status", "msgType"]
    assert tags[-1] == "info"


def test_refuses_to_publish_when_validation_fails(xsd: Path) -> None:
    class Rejecting(CapValidator):
        def errors(self, xml: bytes | str) -> list[str]:
            return ["forced failure"]

    stub = CapStub(validator=Rejecting(xsd), clock=VirtualClock(T0))
    with pytest.raises(CapStubError, match="refusing to publish"):
        stub.build(detection())
    assert stub.rendered == 0


def test_stage_publishes_on_alerts_with_causation(xsd: Path) -> None:
    bus = InMemoryBus()
    stub = CapStub(xsd_path=xsd, clock=VirtualClock(T0))
    runner = StageRunner(bus, stub)
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
    assert out.schema_name == "cap-message"
    assert out.causation_id == env.message_id and out.replay_run_id == "r"
    assert out.stream_time_utc == det.detected_at_stream_utc
    payload = out.payload
    assert isinstance(payload, CAPMessage) and payload.xml is not None


def test_stage_rejects_wrong_payload(xsd: Path) -> None:
    from serac.domain.force_history import ForceHistory

    env: Envelope[BaseModel] = wrap(
        ForceHistory(), topic=topics.DETECTIONS, producer="t", stream_time_utc=T0
    )
    with pytest.raises(CapStubError, match="expected DetectionCandidate"):
        CapStub(xsd_path=xsd).process(
            Received(message_id="1-0", topic=topics.DETECTIONS, envelope=env)
        )
