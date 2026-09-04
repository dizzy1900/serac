"""The real CAP 1.2 generator: a `CascadeForecast` becomes a signed, XSD-valid alert.

This is what `serac.streaming.cap_stub` was a placeholder for. The stub stays where it is --
`validate-stream` asserts on it and the detector stub still needs a CAP stage -- and this
module sits alongside it for the forecast lane.

Everything the message claims is derived from the forecast by a **stated rule**, and the rule
that fired is written into the message as a `parameter` so a recipient can see why it was
told what it was told:

* `status` comes from `confidence_tier` through `STATUS_BY_TIER`. A stub-provenance forecast
  is forced to `Test` whatever its tier says, and `unqualified` is `Test` by construction, so
  "a test tier still produces `status: Test`" is enforced twice.
* `scope` follows `status`: only an `Actual` message is `Public`. Test and Exercise messages
  are `Private`, addressed to the serac operators list, because an exercise that reaches the
  public is not an exercise.
* `urgency` comes from the earliest modelled arrival, `certainty` from the confidence tier,
  and `severity` from the largest modelled peak stage through `SEVERITY_BY_STAGE_M` -- a
  threshold table that is an **assumption**, not a calibrated relation, and says so in
  `serac:severity_rule`.
* `area` is emitted only when the forecast has a footprint or reaches a transect. A footprint
  becomes a CAP `polygon` (CAP orders vertices `lat,lon`, GeoJSON stores `lon,lat`); reached
  transects become `geocode` entries. **No geometry is invented**: a forecast with neither
  gets no `area` and a `serac:area_absent` parameter saying which one was missing.
* Every transect arrival becomes `parameter` blocks: ETA, peak stage, and the lead time
  the alert actually delivers (arrival minus `sent`), each as its own low/best/high triple.

Signing is optional at this layer and applied by `build_alert(..., private_key=...)`; an
unsigned message is still valid CAP and says nothing about who produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from serac.adapters.cap.cap12 import render
from serac.alerting.signing import sign_cap_xml
from serac.domain.cap import (
    CAPArea,
    CAPCertainty,
    CAPInfo,
    CAPKeyValue,
    CAPMessage,
    CAPResponseType,
    CAPScope,
    CAPSeverity,
    CAPStatus,
    CAPUrgency,
)
from serac.domain.common import Range
from serac.domain.forecast import CascadeForecast, ConfidenceTier, ModelProvenance
from serac.domain.geometry import MultiPolygon, Polygon
from serac.errors import SeracError
from serac.validation.cap import CapValidator

GENERATOR_NAME = "serac.alerting.generator"
GENERATOR_VERSION = "0.1.0"
DEFAULT_SENDER = "cap@serac.invalid"
OPERATOR_ADDRESSES = "serac-operators"
DEFAULT_EXPIRES_AFTER = timedelta(hours=6)

STATUS_BY_TIER: dict[ConfidenceTier, CAPStatus] = {
    ConfidenceTier.unqualified: "Test",
    ConfidenceTier.low: "Exercise",
    ConfidenceTier.medium: "Exercise",
    ConfidenceTier.high: "Actual",
}
"""Confidence tier -> CAP status.

`Actual` is reserved for `high`, which no serac model currently reaches: `RELEASE_STATUS.md`
records that nothing is validated against events, and a `low`-confidence surrogate that has
never been cross-validated against a simulator must not put `Actual` on the wire. `Exercise`
is the honest middle: a real message, addressed to operators, that no one should evacuate on.
"""

SEVERITY_BY_STAGE_M: tuple[tuple[float, CAPSeverity], ...] = (
    (5.0, "Extreme"),
    (2.0, "Severe"),
    (0.5, "Moderate"),
    (0.0, "Minor"),
)
"""Largest modelled peak stage -> CAP severity. **An assumption.**

These thresholds are not derived from a depth-consequence study for this corridor; none was
fetched. They are round numbers chosen so that a metre-scale stage is not called `Extreme`
and a five-metre stage is not called `Minor`. `serac:severity_rule` says so in the message.
"""

CERTAINTY_BY_TIER: dict[ConfidenceTier, CAPCertainty] = {
    ConfidenceTier.unqualified: "Unknown",
    ConfidenceTier.low: "Possible",
    ConfidenceTier.medium: "Likely",
    ConfidenceTier.high: "Likely",
}
"""Confidence tier -> CAP certainty. `Observed` is never emitted: serac forecasts, it does
not observe, and CAP 1.2 defines `Observed` as "determined to have occurred"."""

IMMEDIATE_WITHIN_MIN = 30.0
EXPECTED_WITHIN_MIN = 180.0

RESPONSE_BY_STATUS: dict[CAPStatus, list[CAPResponseType]] = {
    "Test": ["None"],
    "Exercise": ["Monitor"],
    "Actual": ["Evacuate", "Monitor"],
    "System": ["None"],
    "Draft": ["None"],
}

SCOPE_BY_STATUS: dict[CAPStatus, CAPScope] = {
    "Test": "Private",
    "Exercise": "Private",
    "Actual": "Public",
    "System": "Private",
    "Draft": "Private",
}

NOT_VALIDATED_NOTE = (
    "serac has no model validated against events (RELEASE_STATUS.md). This message reports "
    "what a model produced; it is not evidence that the model is right."
)


class CapGenerationError(SeracError):
    """The generator refused to produce a message, or produced one the XSD rejected."""


def _identifier(forecast: CascadeForecast, sent: datetime) -> str:
    """CAP 1.2 forbids whitespace, commas, '<' and '&' in an identifier."""
    stamp = sent.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"serac-cascade-{forecast.forecast_id}-{stamp}"


def _triple(value: Range) -> str:
    best = "null" if value.best is None else f"{value.best:g}"
    return f"low={value.low:g} best={best} high={value.high:g} unit={value.unit}"


def status_for(forecast: CascadeForecast) -> tuple[CAPStatus, str]:
    """The CAP status and the sentence explaining which rule produced it."""
    tier = forecast.confidence_tier
    status = STATUS_BY_TIER[tier]
    rule = f"confidence_tier={tier} -> status={status} (serac.alerting.generator.STATUS_BY_TIER)"
    if forecast.model.provenance == ModelProvenance.stub:
        return "Test", (
            f"model.provenance=stub forces status=Test regardless of tier; {rule} would "
            "otherwise apply"
        )
    return status, rule


def severity_for(forecast: CascadeForecast) -> tuple[CAPSeverity, str]:
    """Severity from the largest modelled peak stage, or `Unknown` when none was modelled."""
    stages = [
        arrival.peak_stage_m.high
        for arrival in forecast.transect_arrivals
        if arrival.peak_stage_m is not None
    ]
    if not stages:
        return "Unknown", (
            "no transect arrival carries a peak stage, so severity is Unknown rather than guessed"
        )
    largest = max(stages)
    for threshold, severity in SEVERITY_BY_STAGE_M:
        if largest >= threshold:
            return severity, (
                f"largest modelled peak stage {largest:.2f} m (95th percentile) >= {threshold:g} m "
                f"-> {severity}. THRESHOLDS ARE AN ASSUMPTION: no depth-consequence study for "
                "this corridor was fetched, so they are stated round numbers, not a calibrated "
                "relation."
            )
    return "Unknown", f"peak stage {largest:.2f} m fell through the threshold table"


def urgency_for(forecast: CascadeForecast) -> tuple[CAPUrgency, str]:
    """Urgency from the earliest modelled arrival after `origin_time_utc`."""
    arrivals = [a.arrival_time_min.low for a in forecast.transect_arrivals]
    if not arrivals:
        return "Unknown", "the forecast reaches no transect, so no arrival time bounds urgency"
    earliest = min(arrivals)
    if earliest <= IMMEDIATE_WITHIN_MIN:
        band = "Immediate"
    elif earliest <= EXPECTED_WITHIN_MIN:
        band = "Expected"
    else:
        band = "Future"
    urgency: CAPUrgency = band  # type: ignore[assignment]
    return urgency, (
        f"earliest modelled arrival {earliest:.1f} min after origin (5th percentile) -> {band}"
    )


def area_for(forecast: CascadeForecast, aoi_name: str | None = None) -> tuple[CAPArea | None, str]:
    """A CAP `area` from the footprint and the reached transects, or None and the reason why."""
    reached = [a.transect_id for a in forecast.transect_arrivals]
    polygons = _polygons(forecast.footprint)
    if not polygons and not reached:
        return None, (
            "no area: the forecast carries neither a footprint polygon nor a reached transect. "
            "serac emits no area rather than inventing one (ADR-0012)."
        )
    where = aoi_name or forecast.aoi_id
    if reached:
        description = f"{where}: modelled to reach transect(s) {', '.join(reached)}"
    else:
        description = f"{where}: modelled inundation footprint"
    geocode = [CAPKeyValue(value_name="serac:aoi_id", value=forecast.aoi_id)]
    geocode += [CAPKeyValue(value_name="serac:transect_id", value=t) for t in reached]
    reason = (
        f"area from {len(polygons)} footprint polygon(s) and {len(reached)} reached transect(s)"
        if polygons
        else (
            f"area describes {len(reached)} reached transect(s) by geocode; the forecast has no "
            "footprint polygon, so no CAP polygon is emitted"
        )
    )
    return CAPArea(area_desc=description, polygon=polygons, geocode=geocode), reason


def _polygons(footprint: Polygon | MultiPolygon | None) -> list[str]:
    """GeoJSON `lon,lat` rings -> CAP `lat,lon` vertex strings, outer rings only.

    CAP 1.2 has no way to express a hole, so interior rings are dropped rather than rendered
    as if they were land.
    """
    if footprint is None:
        return []
    if isinstance(footprint, Polygon):
        rings = [footprint.coordinates[0]]
    else:
        rings = [polygon[0] for polygon in footprint.coordinates if polygon]
    out: list[str] = []
    for ring in rings:
        vertices = " ".join(f"{p[1]:.6f},{p[0]:.6f}" for p in ring)
        if vertices:
            out.append(vertices)
    return out


def _arrival_parameters(forecast: CascadeForecast, sent: datetime) -> list[CAPKeyValue]:
    out: list[CAPKeyValue] = []
    for arrival in forecast.transect_arrivals:
        transect = arrival.transect_id
        out.append(
            CAPKeyValue(
                value_name=f"serac:eta_min:{transect}", value=_triple(arrival.arrival_time_min)
            )
        )
        for absolute, label in _absolute_eta(forecast, arrival.arrival_time_min):
            out.append(CAPKeyValue(value_name=f"serac:eta_utc_{label}:{transect}", value=absolute))
        if arrival.peak_stage_m is not None:
            out.append(
                CAPKeyValue(
                    value_name=f"serac:peak_stage_m:{transect}", value=_triple(arrival.peak_stage_m)
                )
            )
        if arrival.peak_discharge_m3s is not None:
            out.append(
                CAPKeyValue(
                    value_name=f"serac:peak_discharge_m3s:{transect}",
                    value=_triple(arrival.peak_discharge_m3s),
                )
            )
        delivered = delivered_lead_time_min(forecast, arrival.arrival_time_min, sent)
        out.append(
            CAPKeyValue(
                value_name=f"serac:delivered_lead_time_min:{transect}",
                value=_triple(delivered),
            )
        )
    return out


def _absolute_eta(forecast: CascadeForecast, arrival: Range) -> list[tuple[str, str]]:
    origin = forecast.origin_time_utc
    out: list[tuple[str, str]] = []
    for label, minutes in (("earliest", arrival.low), ("latest", arrival.high)):
        out.append(((origin + timedelta(minutes=minutes)).isoformat(), label))
    return [(value, label) for value, label in out]


def delivered_lead_time_min(forecast: CascadeForecast, arrival: Range, sent: datetime) -> Range:
    """Minutes between this message being sent and the modelled arrival.

    Negative means the flow has already arrived by the time the message goes out; that is
    reported as a negative number, not clipped to zero.
    """
    elapsed = (sent - forecast.origin_time_utc).total_seconds() / 60.0
    values = sorted(v - elapsed for v in (arrival.low, arrival.high))
    best = None if arrival.best is None else arrival.best - elapsed
    return Range(
        low=values[0],
        high=values[1],
        best=best,
        unit="min",
        source_refs=list(arrival.source_refs),
        notes=(
            f"modelled arrival minus {elapsed:.2f} min elapsed between origin_time_utc and this "
            "message's sent time; negative means the flow arrives before the alert does"
        ),
    )


@dataclass(frozen=True)
class AlertBuild:
    """The message plus the rules that produced it, so a report can quote them."""

    message: CAPMessage
    status_rule: str
    severity_rule: str
    urgency_rule: str
    area_rule: str
    signed: bool


def build_alert(
    forecast: CascadeForecast,
    *,
    sent: datetime,
    sender: str = DEFAULT_SENDER,
    aoi_name: str | None = None,
    expires_after: timedelta = DEFAULT_EXPIRES_AFTER,
    private_key: Ed25519PrivateKey | None = None,
    validator: CapValidator | None = None,
    xsd_path: Path | None = None,
) -> AlertBuild:
    """Render, XSD-validate and optionally sign one CAP alert for one forecast."""
    if sent.tzinfo is None:
        raise CapGenerationError("sent must be a timezone-aware datetime")
    status, status_rule = status_for(forecast)
    severity, severity_rule = severity_for(forecast)
    urgency, urgency_rule = urgency_for(forecast)
    certainty = CERTAINTY_BY_TIER[forecast.confidence_tier]
    area, area_rule = area_for(forecast, aoi_name)

    parameters: list[CAPKeyValue] = [
        CAPKeyValue(value_name="serac:generator", value=f"{GENERATOR_NAME} v{GENERATOR_VERSION}"),
        CAPKeyValue(value_name="serac:forecast_id", value=forecast.forecast_id),
        CAPKeyValue(value_name="serac:aoi_id", value=forecast.aoi_id),
        CAPKeyValue(value_name="serac:event_id", value=forecast.event_id or "null"),
        CAPKeyValue(value_name="serac:detection_id", value=forecast.detection_id or "null"),
        CAPKeyValue(
            value_name="serac:model",
            value=(
                f"{forecast.model.name} v{forecast.model.version} "
                f"provenance={forecast.model.provenance} run_id={forecast.model.run_id or 'null'}"
            ),
        ),
        CAPKeyValue(value_name="serac:confidence_tier", value=str(forecast.confidence_tier)),
        CAPKeyValue(value_name="serac:status_rule", value=status_rule),
        CAPKeyValue(value_name="serac:severity_rule", value=severity_rule),
        CAPKeyValue(value_name="serac:urgency_rule", value=urgency_rule),
        CAPKeyValue(value_name="serac:area_rule", value=area_rule),
        CAPKeyValue(value_name="serac:origin_time_utc", value=forecast.origin_time_utc.isoformat()),
        CAPKeyValue(value_name="serac:source_volume_m3", value=_triple(forecast.source_volume_m3)),
        CAPKeyValue(value_name="serac:runout_km", value=_triple(forecast.runout_km)),
        CAPKeyValue(
            value_name="serac:transects_reached", value=str(len(forecast.transect_arrivals))
        ),
    ]
    if forecast.source_location is not None:
        position = forecast.source_location.coordinates
        lon, lat = position[0], position[1]
        parameters.append(
            CAPKeyValue(value_name="serac:source_location", value=f"{lat:.6f},{lon:.6f}")
        )
    else:
        parameters.append(CAPKeyValue(value_name="serac:source_location", value="null"))
    parameters += _arrival_parameters(forecast, sent)
    parameters += _damming_parameters(forecast)
    if area is None:
        parameters.append(CAPKeyValue(value_name="serac:area_absent", value=area_rule))
    for index, assumption in enumerate(forecast.assumptions, start=1):
        parameters.append(
            CAPKeyValue(value_name=f"serac:forecast_assumption:{index:02d}", value=assumption)
        )

    onset = _onset(forecast)
    info = CAPInfo(
        language="en-US",
        category=["Geo", "Safety"],
        event="High-mountain rock-ice avalanche cascade forecast",
        response_type=RESPONSE_BY_STATUS[status],
        urgency=urgency,
        severity=severity,
        certainty=certainty,
        event_code=[CAPKeyValue(value_name="serac:stage", value="cascade-forecast")],
        effective=sent,
        onset=onset,
        expires=sent + expires_after,
        sender_name="serac",
        headline=_headline(forecast, status),
        description=_description(forecast, status, status_rule),
        instruction=_instruction(status),
        parameter=parameters,
        area=[area] if area is not None else [],
    )
    message = CAPMessage(
        identifier=_identifier(forecast, sent),
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

    xml = render(message)
    checker = validator or CapValidator(xsd_path)
    problems = checker.errors(xml)
    if problems:
        raise CapGenerationError(
            f"refusing to publish {message.identifier}: CAP 1.2 XSD errors: {problems}"
        )
    signed = False
    if private_key is not None:
        xml = sign_cap_xml(xml, private_key)
        problems = checker.errors(xml)
        if problems:
            raise CapGenerationError(
                f"{message.identifier}: the signed rendering failed the CAP 1.2 XSD: {problems}"
            )
        signed = True
    return AlertBuild(
        message=message.model_copy(update={"xml": xml.decode("utf-8")}),
        status_rule=status_rule,
        severity_rule=severity_rule,
        urgency_rule=urgency_rule,
        area_rule=area_rule,
        signed=signed,
    )


def _damming_parameters(forecast: CascadeForecast) -> list[CAPKeyValue]:
    damming = forecast.damming
    if damming is None:
        return [CAPKeyValue(value_name="serac:damming", value="null")]
    out = [CAPKeyValue(value_name="serac:damming_probability", value=_triple(damming.probability))]
    for name, value in (
        ("dam_height_m", damming.dam_height_m),
        ("lake_volume_m3", damming.lake_volume_m3),
        ("breach_time_after_formation_min", damming.breach_time_after_formation_min),
    ):
        if value is not None:
            out.append(CAPKeyValue(value_name=f"serac:{name}", value=_triple(value)))
    for surge in damming.secondary_surges:
        out.append(
            CAPKeyValue(
                value_name=f"serac:secondary_surge_min:{surge.transect_id}",
                value=(
                    f"{_triple(surge.arrival_after_breach_min)} after breach; "
                    f"origin_chainage_km={surge.origin_chainage_km:g}; label={surge.label}"
                ),
            )
        )
    return out


def _onset(forecast: CascadeForecast) -> datetime | None:
    arrivals = [a.arrival_time_min.low for a in forecast.transect_arrivals]
    if not arrivals:
        return None
    return forecast.origin_time_utc + timedelta(minutes=min(arrivals))


def _headline(forecast: CascadeForecast, status: CAPStatus) -> str:
    prefix = "TEST: " if status == "Test" else ("EXERCISE: " if status == "Exercise" else "")
    reached = len(forecast.transect_arrivals)
    body = (
        f"cascade forecast {forecast.forecast_id}: {reached} transect(s) reached, "
        f"confidence {forecast.confidence_tier}"
    )
    return (prefix + body)[:160]


def _description(forecast: CascadeForecast, status: CAPStatus, status_rule: str) -> str:
    lines = [
        f"Modelled rock-ice avalanche cascade in AOI {forecast.aoi_id}, "
        f"origin {forecast.origin_time_utc.isoformat()}.",
        f"Model {forecast.model.name} v{forecast.model.version} "
        f"(provenance {forecast.model.provenance}), confidence tier "
        f"{forecast.confidence_tier}; CAP status {status} because {status_rule}.",
    ]
    if forecast.transect_arrivals:
        for arrival in forecast.transect_arrivals:
            window = arrival.arrival_time_min
            lines.append(
                f"  {arrival.transect_id}: arrival {window.low:.1f}-{window.high:.1f} min after "
                "origin"
                + (
                    f", peak stage {arrival.peak_stage_m.low:.2f}-{arrival.peak_stage_m.high:.2f} m"
                    if arrival.peak_stage_m is not None
                    else ""
                )
            )
    else:
        lines.append(
            "  No transect in this AOI is reached by the forecast. That is the model's output, "
            "not a statement that the corridor is safe."
        )
    lines.append(NOT_VALIDATED_NOTE)
    return "\n".join(lines)


def _instruction(status: CAPStatus) -> str:
    if status == "Test":
        return "No action. This is a test message."
    if status == "Exercise":
        return (
            "Exercise only. Do not act on this message operationally. It is addressed to serac "
            "operators so that the alerting path can be observed end to end."
        )
    return (
        "Consult the responsible authority before acting. serac is a modelling system, not an "
        "official warning authority for any jurisdiction."
    )
