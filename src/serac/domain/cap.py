"""Common Alerting Protocol v1.2 message contract.

Field names are snake_case Python equivalents of the CAP element names (`msgType` becomes
`msg_type`, `responseType` becomes `response_type`); the XML renderer in
`serac.adapters.cap` maps them back. Enumerations reproduce the CAP 1.2 code lists exactly
(OASIS CAP-v1.2, section 3.2), so a message that validates here can be rendered to XML that
validates against the vendored `contracts/vendor/cap/CAP-v1.2.xsd`.

Design constraint carried over from the plan: a CAP message with no `area` is legal and is
the *only* form the Prompt 1 stub emits, because no stage has a location to report.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CAP_CONTRACT_VERSION = "0.1.0"

CAPStatus = Literal["Actual", "Exercise", "System", "Test", "Draft"]
CAPMsgType = Literal["Alert", "Update", "Cancel", "Ack", "Error"]
CAPScope = Literal["Public", "Restricted", "Private"]
CAPCategory = Literal[
    "Geo",
    "Met",
    "Safety",
    "Security",
    "Rescue",
    "Fire",
    "Health",
    "Env",
    "Transport",
    "Infra",
    "CBRNE",
    "Other",
]
CAPResponseType = Literal[
    "Shelter", "Evacuate", "Prepare", "Execute", "Avoid", "Monitor", "Assess", "AllClear", "None"
]
CAPUrgency = Literal["Immediate", "Expected", "Future", "Past", "Unknown"]
CAPSeverity = Literal["Extreme", "Severe", "Moderate", "Minor", "Unknown"]
CAPCertainty = Literal["Observed", "Likely", "Possible", "Unlikely", "Unknown"]

# CAP 1.2 forbids spaces, commas, '<' and '&' in identifier, sender and references.
_CAP_TOKEN = r"^[^\s,<&]+$"


class CAPKeyValue(BaseModel):
    """`eventCode`, `parameter` and `geocode` share the valueName/value shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value_name: str = Field(min_length=1)
    value: str


class CAPResource(BaseModel):
    """A CAP `resource` block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_desc: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    size: int | None = Field(default=None, ge=0)
    uri: str | None = None
    deref_uri: str | None = None
    digest: str | None = None


class CAPArea(BaseModel):
    """A CAP `area` block. Polygons are `lat,lon` pairs; circles `lat,lon radius_km`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    area_desc: str = Field(min_length=1)
    polygon: list[str] = Field(default_factory=list)
    circle: list[str] = Field(default_factory=list)
    geocode: list[CAPKeyValue] = Field(default_factory=list)
    altitude: float | None = None
    ceiling: float | None = None

    @model_validator(mode="after")
    def _ceiling_needs_altitude(self) -> Self:
        if self.ceiling is not None and self.altitude is None:
            raise ValueError("ceiling requires altitude (CAP 1.2 3.2.4)")
        return self


class CAPInfo(BaseModel):
    """A CAP `info` block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str = "en-US"
    category: list[CAPCategory] = Field(min_length=1)
    event: str = Field(min_length=1)
    response_type: list[CAPResponseType] = Field(default_factory=list)
    urgency: CAPUrgency
    severity: CAPSeverity
    certainty: CAPCertainty
    audience: str | None = None
    event_code: list[CAPKeyValue] = Field(default_factory=list)
    effective: AwareDatetime | None = None
    onset: AwareDatetime | None = None
    expires: AwareDatetime | None = None
    sender_name: str | None = None
    headline: str | None = Field(default=None, max_length=160)
    description: str | None = None
    instruction: str | None = None
    web: str | None = None
    contact: str | None = None
    parameter: list[CAPKeyValue] = Field(default_factory=list)
    resource: list[CAPResource] = Field(default_factory=list)
    area: list[CAPArea] = Field(
        default_factory=list,
        description="Empty unless a stage has a sourced footprint; the stub never fills it.",
    )


class CAPMessage(BaseModel):
    """A CAP 1.2 `alert` element."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CAP_CONTRACT_VERSION
    identifier: str = Field(pattern=_CAP_TOKEN)
    sender: str = Field(pattern=_CAP_TOKEN)
    sent: AwareDatetime
    status: CAPStatus
    msg_type: CAPMsgType
    source: str | None = None
    scope: CAPScope
    restriction: str | None = None
    addresses: str | None = None
    code: list[str] = Field(default_factory=list)
    note: str | None = None
    references: str | None = None
    incidents: str | None = None
    info: list[CAPInfo] = Field(default_factory=list)
    xml: str | None = Field(
        default=None, description="Rendered CAP XML when a renderer produced this message."
    )

    @model_validator(mode="after")
    def _scope_rules(self) -> Self:
        if self.scope == "Restricted" and not self.restriction:
            raise ValueError("scope=Restricted requires restriction (CAP 1.2 3.2.1)")
        if self.scope == "Private" and not self.addresses:
            raise ValueError("scope=Private requires addresses (CAP 1.2 3.2.1)")
        if self.msg_type in ("Update", "Cancel", "Ack", "Error") and not self.references:
            raise ValueError(f"msgType={self.msg_type} requires references (CAP 1.2 3.2.1)")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"cap-message": CAPMessage}
