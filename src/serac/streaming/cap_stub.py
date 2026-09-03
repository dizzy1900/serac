"""CAP stage: `serac.detections` -> `serac.alerts` as schema-valid CAP 1.2 (stub form).

Every message this stage emits is `status=Test`, `scope=Private` (addressed to the serac
operators list), sent by `serac-stub@serac.invalid`, with `urgency`, `severity` and
`certainty` all `Unknown` and **no `area` element**: the detector has no location, so no
footprint is invented (ADR-0012). The rendered XML is validated against the vendored
CAP 1.2 XSD before publishing; an invalid rendering raises instead of reaching the bus.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from serac.adapters.cap.cap12 import render
from serac.domain import topics
from serac.domain.cap import CAPInfo, CAPKeyValue, CAPMessage
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.errors import SeracError
from serac.ports.bus import Received
from serac.ports.clock import Clock, WallClock
from serac.streaming.stage import Stage
from serac.validation.cap import CapValidator

STUB_SENDER = "serac-stub@serac.invalid"
STUB_ADDRESSES = "serac-operators"
STUB_EVENT = "Long-period seismic signal candidate (STUB detector, unvalidated)"
STUB_NOTE = (
    "STUB — replaced in Prompt 2. Test message from a placeholder energy-ratio detector: "
    "no discriminator, no location, no validated performance. Not an alert."
)
DEFAULT_EXPIRES_AFTER = timedelta(hours=1)


class CapStubError(SeracError):
    """The stage could not produce a valid CAP message."""


def cap_message_for(
    detection: DetectionCandidate,
    *,
    sent: datetime,
    expires_after: timedelta = DEFAULT_EXPIRES_AFTER,
) -> CAPMessage:
    """Build the stub CAP message for a detection. `sent` is an aware datetime."""
    if sent.tzinfo is None:
        raise CapStubError("sent must be a timezone-aware datetime")
    identifier = "serac-stub-" + detection.detection_id.replace("/", "_").replace(":", "")
    parameters = [
        CAPKeyValue(value_name="serac:is_stub", value="true"),
        CAPKeyValue(value_name="serac:detection_id", value=detection.detection_id),
        CAPKeyValue(value_name="serac:sncl", value=detection.sncl.key),
        CAPKeyValue(value_name="serac:detector", value=detection.detector),
        CAPKeyValue(value_name="serac:detector_version", value=detection.detector_version),
        CAPKeyValue(value_name="serac:score", value=repr(detection.score)),
        CAPKeyValue(value_name="serac:threshold", value=repr(detection.threshold)),
        CAPKeyValue(
            value_name="serac:detected_at_stream_utc",
            value=detection.detected_at_stream_utc.isoformat(),
        ),
        CAPKeyValue(value_name="serac:source_location", value="null"),
    ]
    info = CAPInfo(
        language="en-US",
        category=["Geo"],
        event=STUB_EVENT,
        response_type=["None"],
        urgency="Unknown",
        severity="Unknown",
        certainty="Unknown",
        event_code=[CAPKeyValue(value_name="serac:stage", value="detector-stub")],
        onset=detection.detected_at_stream_utc,
        expires=sent + expires_after,
        sender_name="serac (stub detector)",
        headline="TEST: placeholder detector candidate; not an alert",
        description=(
            f"Placeholder LP/SP energy-ratio candidate on {detection.sncl.key}: score "
            f"{detection.score:.4g} vs placeholder threshold {detection.threshold:.4g}. "
            "The detector is a stub with no discriminator and no location."
        ),
        instruction="No action. This is a test message from a stub.",
        parameter=parameters,
        area=[],
    )
    return CAPMessage(
        identifier=identifier,
        sender=STUB_SENDER,
        sent=sent,
        status="Test",
        msg_type="Alert",
        source="serac.streaming.cap_stub",
        scope="Private",
        addresses=STUB_ADDRESSES,
        note=STUB_NOTE,
        info=[info],
    )


class CapStub(Stage):
    """`serac.detections` -> `serac.alerts`, refusing to publish an XSD-invalid message."""

    name = "cap-stub"
    input_topic = topics.DETECTIONS
    group = "cap"

    def __init__(
        self,
        *,
        validator: CapValidator | None = None,
        xsd_path: Path | None = None,
        clock: Clock | None = None,
        expires_after: timedelta = DEFAULT_EXPIRES_AFTER,
    ) -> None:
        self.validator = validator or CapValidator(xsd_path)
        self.clock = clock or WallClock()
        self.expires_after = expires_after
        self.rendered = 0

    def build(self, detection: DetectionCandidate) -> CAPMessage:
        """Render, validate and return the message with its XML attached."""
        message = cap_message_for(
            detection, sent=self.clock.now(), expires_after=self.expires_after
        )
        xml = render(message)
        problems = self.validator.errors(xml)
        if problems:
            raise CapStubError(
                f"refusing to publish {message.identifier}: CAP 1.2 XSD errors: {problems}"
            )
        self.rendered += 1
        return message.model_copy(update={"xml": xml.decode("utf-8")})

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        detection = received.envelope.payload
        if not isinstance(detection, DetectionCandidate):
            raise CapStubError(
                f"expected DetectionCandidate on {self.input_topic}, got {type(detection).__name__}"
            )
        message = self.build(detection)
        return [
            wrap(
                message,
                topic=topics.ALERTS,
                producer=self.name,
                stream_time_utc=detection.detected_at_stream_utc,
                causation_id=received.envelope.message_id,
                replay_run_id=received.envelope.replay_run_id,
            )
        ]
