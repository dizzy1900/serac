"""CAP stage: detections (and forecasts, if the lane ever carries them) -> XSD-valid CAP 1.2.

This is the default CAP stage on the replay and live stream lanes. It renders through
`serac.adapters.cap.cap12.render` — the same XSD path as `serac.alerting.generator` — and
optionally signs with Ed25519 when `SERAC_CAP_SIGNING_KEY` points at a readable key.

Detection-path messages are always `status=Test`, `scope=Private`, with Unknown
urgency/severity/certainty and **no `area`**. The detector is not validated
(`RELEASE_STATUS.md`); `DetectionCandidate.source_location` is often None; no arrival time,
polygon, or severity is invented from a detection. That is the same no-geometry rule as
`serac.alerting.generator.area_for`.

A `CascadeForecast` payload is handed to `build_alert` so `STATUS_BY_TIER` applies. High/Actual
remains unreachable from current models; this stage does not change that table.

`CapStub` stays in the tree. Select it with `--cap stub` when a fixture pins stub XML.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel

from serac.adapters.cap.cap12 import render
from serac.alerting.generator import (
    DEFAULT_EXPIRES_AFTER,
    DEFAULT_SENDER,
    NOT_VALIDATED_NOTE,
    OPERATOR_ADDRESSES,
    RESPONSE_BY_STATUS,
    SCOPE_BY_STATUS,
    CapGenerationError,
    build_alert,
)
from serac.alerting.keys import load_signing_key_if_present
from serac.alerting.signing import sign_cap_xml
from serac.domain import topics
from serac.domain.cap import CAPInfo, CAPKeyValue, CAPMessage, CAPStatus
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.forecast import CascadeForecast
from serac.errors import SeracError
from serac.ports.bus import Received
from serac.ports.clock import Clock, WallClock
from serac.streaming.cap_stub import CapStub
from serac.streaming.stage import Stage
from serac.validation.cap import CapValidator

CapKind = Literal["xsd", "stub"]

GENERATOR_NAME = "serac.streaming.cap_stage"
GENERATOR_VERSION = "0.1.0"
STAGE_NAME = "cap"
DETECTION_EVENT = (
    "Seismic mass-movement detection candidate (research output, not an operational alert)"
)
DETECTION_INSTRUCTION = (
    "No action. This is research output from an unvalidated detector, not an operational "
    "alert. serac is not a warning authority in any jurisdiction."
)
DETECTION_HEADLINE = "TEST: detection candidate; research output, not an operational alert"
DETECTION_STATUS_RULE = (
    "detection-path messages are status=Test while no detector is validated "
    "(CLAUDE.md; RELEASE_STATUS.md). STATUS_BY_TIER is not applied: a DetectionCandidate "
    "has no confidence_tier."
)
LANE_DISCLAIMER = (
    "status=Test, scope=Private. This is research output, not an operational alert system."
)


class CapStageError(SeracError):
    """The stage could not produce a valid CAP message."""


def parse_cap_kind(value: str) -> CapKind:
    """`xsd` (real generator, the default) or `stub` (`CapStub`)."""
    kind = value.strip().lower()
    if kind == "xsd":
        return "xsd"
    if kind == "stub":
        return "stub"
    raise ValueError(f"cap must be 'xsd' or 'stub', got {value!r}")


def cap_lane_banner(kind: CapKind) -> str:
    """One line the CLI prints so an operator cannot miss that this is not an alert system."""
    if kind == "stub":
        return f"CAP stage=cap-stub; {LANE_DISCLAIMER}"
    return f"CAP stage=cap (CAP 1.2 XSD generator); {LANE_DISCLAIMER}"


def _identifier_from_detection(detection: DetectionCandidate, sent: datetime) -> str:
    """CAP 1.2 forbids whitespace, commas, '<' and '&' in an identifier."""
    stamp = sent.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = detection.detection_id
    for char in (" ", ",", "<", "&", "/"):
        raw = raw.replace(char, "_")
    raw = raw.replace(":", "")
    return f"serac-detection-{raw}-{stamp}"


def _source_location_parameter(detection: DetectionCandidate) -> CAPKeyValue:
    location = detection.source_location
    if location is None:
        return CAPKeyValue(value_name="serac:source_location", value="null")
    # A point from an inversion is recorded as a parameter. It is not turned into a CAP
    # polygon or circle: that would invent geometry the detection does not carry.
    return CAPKeyValue(
        value_name="serac:source_location",
        value=(
            f"{location.latitude:.6f},{location.longitude:.6f} "
            f"method={location.method} grid_spacing_km={location.grid_spacing_km:g}"
        ),
    )


def _detection_parameters(detection: DetectionCandidate) -> list[CAPKeyValue]:
    parameters = [
        CAPKeyValue(value_name="serac:generator", value=f"{GENERATOR_NAME} v{GENERATOR_VERSION}"),
        CAPKeyValue(value_name="serac:is_stub", value="true" if detection.is_stub else "false"),
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
        CAPKeyValue(value_name="serac:status_rule", value=DETECTION_STATUS_RULE),
        _source_location_parameter(detection),
        CAPKeyValue(
            value_name="serac:area_absent",
            value=(
                "no area: a DetectionCandidate is not a cascade footprint. serac emits no "
                "area rather than inventing a polygon or circle (ADR-0012, ADR-0017)."
            ),
        ),
    ]
    if detection.probability is not None:
        parameters.append(
            CAPKeyValue(
                value_name="serac:probability",
                value=(
                    f"{detection.probability:g} calibration={detection.probability_calibration}"
                ),
            )
        )
    if detection.class_label is not None:
        parameters.append(CAPKeyValue(value_name="serac:class_label", value=detection.class_label))
    return parameters


def _detection_description(detection: DetectionCandidate) -> str:
    stub_note = " (placeholder stub)" if detection.is_stub else ""
    lines = [
        f"Detection candidate {detection.detection_id} on {detection.sncl.key}.",
        (
            f"Detector {detection.detector} v{detection.detector_version}{stub_note}: "
            f"score {detection.score:.4g} vs threshold {detection.threshold:.4g}."
        ),
        (
            "This is research output from a detector that has not been validated against "
            "events. It is not an operational alert."
        ),
        NOT_VALIDATED_NOTE,
    ]
    if detection.notes:
        lines.append(detection.notes)
    return "\n".join(lines)


def cap_message_for_detection(
    detection: DetectionCandidate,
    *,
    sent: datetime,
    expires_after: timedelta = DEFAULT_EXPIRES_AFTER,
    sender: str = DEFAULT_SENDER,
) -> CAPMessage:
    """Map a detection to Test/Private CAP. `sent` must be timezone-aware.

    Reuses generator constants (`SCOPE_BY_STATUS`, `RESPONSE_BY_STATUS`, sender, operators
    list, the not-validated note) that do not require a `CascadeForecast`. Does not call
    `severity_for`, `urgency_for`, or `area_for`: those derive from modelled stage and
    arrivals a detection does not have.
    """
    if sent.tzinfo is None:
        raise CapStageError("sent must be a timezone-aware datetime")
    status: CAPStatus = "Test"
    info = CAPInfo(
        language="en-US",
        category=["Geo"],
        event=DETECTION_EVENT,
        response_type=RESPONSE_BY_STATUS[status],
        urgency="Unknown",
        severity="Unknown",
        certainty="Unknown",
        event_code=[CAPKeyValue(value_name="serac:stage", value="detection")],
        effective=sent,
        onset=detection.detected_at_stream_utc,
        expires=sent + expires_after,
        sender_name="serac",
        headline=DETECTION_HEADLINE,
        description=_detection_description(detection),
        instruction=DETECTION_INSTRUCTION,
        parameter=_detection_parameters(detection),
        area=[],
    )
    return CAPMessage(
        identifier=_identifier_from_detection(detection, sent),
        sender=sender,
        sent=sent,
        status=status,
        msg_type="Alert",
        source=GENERATOR_NAME,
        scope=SCOPE_BY_STATUS[status],
        addresses=OPERATOR_ADDRESSES if SCOPE_BY_STATUS[status] == "Private" else None,
        note=NOT_VALIDATED_NOTE,
        info=[info],
    )


class CapStage(Stage):
    """`serac.detections` -> `serac.alerts` via the real CAP 1.2 renderer."""

    name = STAGE_NAME
    input_topic = topics.DETECTIONS
    group = "cap"

    def __init__(
        self,
        *,
        validator: CapValidator | None = None,
        xsd_path: Path | None = None,
        clock: Clock | None = None,
        expires_after: timedelta = DEFAULT_EXPIRES_AFTER,
        private_key: Ed25519PrivateKey | None = None,
        load_key_from_env: bool = True,
    ) -> None:
        self.validator = validator or CapValidator(xsd_path)
        self.clock = clock or WallClock()
        self.expires_after = expires_after
        if private_key is not None:
            self.private_key: Ed25519PrivateKey | None = private_key
        elif load_key_from_env:
            self.private_key = load_signing_key_if_present()
        else:
            self.private_key = None
        self.rendered = 0
        self.signed = 0

    def _attach_xml(self, message: CAPMessage, xml: bytes) -> CAPMessage:
        problems = self.validator.errors(xml)
        if problems:
            raise CapStageError(
                f"refusing to publish {message.identifier}: CAP 1.2 XSD errors: {problems}"
            )
        signed_xml = xml
        if self.private_key is not None:
            signed_xml = sign_cap_xml(xml, self.private_key)
            problems = self.validator.errors(signed_xml)
            if problems:
                raise CapStageError(
                    f"{message.identifier}: the signed rendering failed the CAP 1.2 XSD: {problems}"
                )
            self.signed += 1
        self.rendered += 1
        return message.model_copy(update={"xml": signed_xml.decode("utf-8")})

    def build_detection(self, detection: DetectionCandidate) -> CAPMessage:
        """Render, validate, optionally sign a detection-path Test message."""
        message = cap_message_for_detection(
            detection, sent=self.clock.now(), expires_after=self.expires_after
        )
        return self._attach_xml(message, render(message))

    def build_forecast(self, forecast: CascadeForecast) -> CAPMessage:
        """Hand a forecast to `build_alert` so `STATUS_BY_TIER` applies."""
        try:
            built = build_alert(
                forecast,
                sent=self.clock.now(),
                private_key=self.private_key,
                validator=self.validator,
            )
        except CapGenerationError as exc:
            raise CapStageError(str(exc)) from exc
        self.rendered += 1
        if built.signed:
            self.signed += 1
        return built.message

    def build(self, payload: DetectionCandidate | CascadeForecast) -> CAPMessage:
        if isinstance(payload, CascadeForecast):
            return self.build_forecast(payload)
        return self.build_detection(payload)

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        payload = received.envelope.payload
        if isinstance(payload, DetectionCandidate):
            message = self.build_detection(payload)
            stream_time = payload.detected_at_stream_utc
        elif isinstance(payload, CascadeForecast):
            message = self.build_forecast(payload)
            stream_time = payload.issued_utc
        else:
            raise CapStageError(
                f"expected DetectionCandidate or CascadeForecast on {self.input_topic}, "
                f"got {type(payload).__name__}"
            )
        return [
            wrap(
                message,
                topic=topics.ALERTS,
                producer=self.name,
                stream_time_utc=stream_time,
                causation_id=received.envelope.message_id,
                replay_run_id=received.envelope.replay_run_id,
            )
        ]


def build_cap_stage(
    kind: CapKind = "xsd",
    *,
    validator: CapValidator | None = None,
    xsd_path: Path | None = None,
    clock: Clock | None = None,
    private_key: Ed25519PrivateKey | None = None,
    load_key_from_env: bool = True,
) -> Stage:
    """Construct the lane's CAP stage. `stub` is CapStub; default is the real XSD generator."""
    if kind == "stub":
        return CapStub(validator=validator, xsd_path=xsd_path, clock=clock)
    return CapStage(
        validator=validator,
        xsd_path=xsd_path,
        clock=clock,
        private_key=private_key,
        load_key_from_env=load_key_from_env,
    )
