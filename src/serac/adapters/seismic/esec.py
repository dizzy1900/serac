"""Exotic Seismic Events Catalog (ESEC) parser — the positive set for the M1 discriminator.

ESEC is served by the IRIS/EarthScope SPUD service at `https://ds.iris.edu/spudservice/esec`.
The endpoint content-negotiates: with no `Accept` header it returns the XML HTML-escaped
inside a `<pre>` block, and only `Accept: application/xml` yields the real document. The
committed fixture is that real document, byte-for-byte, so the gate parses offline exactly
what the service returned.

**Units.** ESEC states a unit only in a handful of tag names (`MaxdisthfKm`, `LocuncertKm`).
Every other numeric tag — `H`, `L`, `Volume`, `AreaTotal`, `Mass`, `PeakDischarge` — carries
no unit in the document, and the SPUD service publishes no machine-readable schema alongside
it. Rather than assume metres and cubic metres, those fields are parsed as bare floats and
their `*_unit` is `None`. Nothing downstream in M1 consumes them as physical quantities: the
discriminator trains on waveforms, and these fields exist only for the model card's
descriptive tables. Recording a guessed unit would be exactly the fabricated precision the
project forbids.

**Location.** ESEC gives both a nominal `Latitude`/`Longitude` and, for 161 of 319 events, a
`CrownLat`/`CrownLon` (the head scarp). `EsecEvent.location` prefers the crown when present,
because that is the seismic source region for a mass movement, and `location_basis` records
which was used.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.errors import SeracError

ADAPTER_NAME = "EsecSpudCatalog"
ADAPTER_VERSION = "0.1.0"

ESEC_URL = "https://ds.iris.edu/spudservice/esec"
ESEC_ACCEPT_HEADER = "application/xml"
ESEC_FIXTURE = Path("data/fixtures/esec/esec_events_2026-09-03.xml")

# ESEC's own top-level grouping. Kept verbatim (including its inconsistent capitalisation)
# so a value can be traced back to the document without a translation table.
SUBTYPES = (
    "Rock/ice/debris avalanches and slides",
    "Rock/debris falls",
    "Lahar/debris flow/outburst flood",
    "Snow avalanches",
    "mine collapse",
    "flank collapse",
)


class EsecError(SeracError):
    """The ESEC document could not be parsed."""


class EsecSubType(StrEnum):
    """The six `SubType` values present in the 2026-09-03 document."""

    rock_ice_debris_avalanche = "Rock/ice/debris avalanches and slides"
    rock_debris_fall = "Rock/debris falls"
    lahar_debris_flow = "Lahar/debris flow/outburst flood"
    snow_avalanche = "Snow avalanches"
    mine_collapse = "mine collapse"
    flank_collapse = "flank collapse"


class EsecMeasurement(BaseModel):
    """A number ESEC reports, with its low/high bracket and whatever unit the document states.

    `unit` is `None` unless the source tag names one; see the module docstring. `low`/`high`
    are ESEC's own bracket columns, not an uncertainty serac computed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None = None
    low: float | None = None
    high: float | None = None
    unit: str | None = Field(
        default=None, description="Only set when the ESEC tag name states one (e.g. `km`)."
    )
    source_tag: str = Field(min_length=1, description="The ESEC tag this came from.")

    @property
    def is_empty(self) -> bool:
        return self.value is None and self.low is None and self.high is None


class EsecEvent(BaseModel):
    """One ESEC record, typed. Absent tags are `None`; nothing is inferred or defaulted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, description="ESEC `EventId`, e.g. `314`.")
    spud_id: str = Field(min_length=1, description="SPUD row id (the XML `id` attribute).")
    product_id: str = Field(min_length=1, description="ESEC `ProductId`, e.g. `Esec_314`.")
    name: str
    description: str
    sub_type: EsecSubType
    type: str = Field(description="ESEC free-text `Type`, e.g. `rock avalanche`.")

    start_utc: AwareDatetime
    end_utc: AwareDatetime | None = None

    latitude: float = Field(ge=-90, le=90, description="ESEC `Latitude` (nominal).")
    longitude: float = Field(ge=-180, le=180, description="ESEC `Longitude` (nominal).")
    crown_latitude: float | None = Field(default=None, ge=-90, le=90)
    crown_longitude: float | None = Field(default=None, ge=-180, le=180)
    tip_latitude: float | None = Field(default=None, ge=-90, le=90)
    tip_longitude: float | None = Field(default=None, ge=-180, le=180)
    location_uncertainty_km: float | None = Field(default=None, ge=0)

    fall_height: EsecMeasurement | None = Field(default=None, description="ESEC `H`; unit null.")
    runout_length: EsecMeasurement | None = Field(default=None, description="ESEC `L`; unit null.")
    volume: EsecMeasurement | None = None
    mass: EsecMeasurement | None = None
    area_total: EsecMeasurement | None = None

    doi: str | None = None
    link: str | None = None
    data_location: str | None = Field(
        default=None, description="ESEC `Datlocation`: which archives hold the waveforms."
    )
    lp_potential: int | None = Field(
        default=None,
        description="ESEC `Lppotential`: whether the event is expected to show long-period energy.",
    )
    max_distance_lp_km: float | None = Field(default=None, ge=0)
    max_distance_hf_km: float | None = Field(default=None, ge=0)
    data_quality_1to5: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc is not None and self.end_utc < self.start_utc:
            raise ValueError(f"ESEC {self.event_id}: Endtime precedes Starttime")
        return self

    @property
    def location(self) -> tuple[float, float]:
        """Preferred source location: the crown when ESEC gives one, else the nominal point."""
        if self.crown_latitude is not None and self.crown_longitude is not None:
            return self.crown_latitude, self.crown_longitude
        return self.latitude, self.longitude

    @property
    def location_basis(self) -> Literal["esec_crown", "esec_nominal"]:
        if self.crown_latitude is not None and self.crown_longitude is not None:
            return "esec_crown"
        return "esec_nominal"


def _text(element: ET.Element, tag: str) -> str | None:
    """Stripped text of a child tag, or None when absent or empty.

    ESEC writes present-but-empty tags (`<DOI></DOI>` on 40 records), which mean "not
    recorded" exactly as an absent tag does; both map to None.
    """
    child = element.find(tag)
    if child is None:
        return None
    value = (child.text or "").strip()
    return value or None


def _float(element: ET.Element, tag: str) -> float | None:
    raw = _text(element, tag)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(element: ET.Element, tag: str) -> int | None:
    value = _float(element, tag)
    return None if value is None else int(value)


def _measurement(
    element: ET.Element, tag: str, *, unit: str | None = None
) -> EsecMeasurement | None:
    """`<Tag>`, `<TagLow>`, `<TagHigh>` as one measurement, or None when all three are absent."""
    m = EsecMeasurement(
        value=_float(element, tag),
        low=_float(element, f"{tag}Low"),
        high=_float(element, f"{tag}High"),
        unit=unit,
        source_tag=tag,
    )
    return None if m.is_empty else m


def _timestamp(element: ET.Element, tag: str) -> datetime | None:
    """Parse an ESEC naive timestamp as UTC.

    ESEC timestamps carry no zone designator (`2022-07-16T01:54:33.0`). The catalogue is
    published by the IRIS/EarthScope DMC, whose event times are UTC; they are read as UTC and
    that assumption is stated here rather than silently applied.
    """
    raw = _text(element, tag)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_esec_event(element: ET.Element) -> EsecEvent:
    """One `<EsecEvents>` element to an `EsecEvent`."""
    event_id = _text(element, "EventId")
    if event_id is None:
        raise EsecError("ESEC record has no EventId")
    start = _timestamp(element, "Starttime")
    if start is None:
        raise EsecError(f"ESEC {event_id}: unparseable or absent Starttime")
    latitude, longitude = _float(element, "Latitude"), _float(element, "Longitude")
    if latitude is None or longitude is None:
        raise EsecError(f"ESEC {event_id}: absent Latitude/Longitude")
    sub_type_raw = _text(element, "SubType")
    if sub_type_raw not in SUBTYPES:
        raise EsecError(f"ESEC {event_id}: unrecognised SubType {sub_type_raw!r}")

    return EsecEvent(
        event_id=event_id,
        spud_id=str(element.get("id") or event_id),
        product_id=_text(element, "ProductId") or f"Esec_{event_id}",
        name=_text(element, "Name") or "",
        description=_text(element, "Description") or "",
        sub_type=EsecSubType(sub_type_raw),
        type=_text(element, "Type") or "",
        start_utc=start,
        end_utc=_timestamp(element, "Endtime"),
        latitude=latitude,
        longitude=longitude,
        crown_latitude=_float(element, "CrownLat"),
        crown_longitude=_float(element, "CrownLon"),
        tip_latitude=_float(element, "TipLat"),
        tip_longitude=_float(element, "TipLon"),
        location_uncertainty_km=_float(element, "LocuncertKm"),
        fall_height=_measurement(element, "H"),
        runout_length=_measurement(element, "L"),
        volume=_measurement(element, "Volume"),
        mass=_measurement(element, "Mass"),
        area_total=_measurement(element, "AreaTotal"),
        doi=_text(element, "DOI"),
        link=_text(element, "Link"),
        data_location=_text(element, "Datlocation"),
        lp_potential=_int(element, "Lppotential"),
        max_distance_lp_km=_float(element, "MaxdistlpKm"),
        max_distance_hf_km=_float(element, "MaxdisthfKm"),
        data_quality_1to5=_int(element, "Otherdataquality1to5"),
    )


def parse_esec_xml(data: bytes | str) -> list[EsecEvent]:
    """Every `<EsecEvents>` record in a SPUD ESEC document, in document order.

    The `count` attribute on `<Results>` is checked against the number of records parsed, so a
    truncated download fails loudly instead of silently shrinking the positive set.
    """
    try:
        root = ET.fromstring(data if isinstance(data, str) else data.decode("utf-8"))
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise EsecError(f"ESEC document is not parseable XML: {exc}") from exc
    if root.tag != "Results":
        raise EsecError(
            f"expected a <Results> root, got <{root.tag}>. The SPUD endpoint returns the XML "
            f"HTML-escaped inside <pre> unless `Accept: {ESEC_ACCEPT_HEADER}` is sent."
        )
    events = [parse_esec_event(e) for e in root.findall("EsecEvents")]
    declared = root.get("count")
    if declared is not None and int(declared) != len(events):
        raise EsecError(
            f"ESEC document declares count={declared} but {len(events)} records parsed; "
            "the download is truncated"
        )
    return events


def load_esec_fixture(repo_root: Path) -> list[EsecEvent]:
    """Parse the committed ESEC fixture. Offline; used by the dataset build and the gate."""
    path = repo_root / ESEC_FIXTURE
    if not path.exists():
        raise EsecError(f"ESEC fixture missing at {path}; it is required and is committed")
    return parse_esec_xml(path.read_bytes())
