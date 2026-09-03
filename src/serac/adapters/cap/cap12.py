"""Render a `CAPMessage` as CAP 1.2 XML with lxml.

Element order follows the OASIS CAP-v1.2 schema exactly (`alert`, `info`, `resource`, `area`),
because the XSD uses `<sequence>` and a reordered element is a validation failure. Dates are
written in the CAP form `YYYY-MM-DDThh:mm:ss+hh:mm` (the schema pattern forbids fractional
seconds and the `Z` suffix). Rendering never adds an `area`: an empty `CAPInfo.area` list
renders no `area` element, which is the only footprint-free form the Prompt 1 stub emits.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lxml import etree

from serac.domain.cap import CAPArea, CAPInfo, CAPKeyValue, CAPMessage, CAPResource

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"


def cap_datetime(value: datetime) -> str:
    """CAP 1.2 dateTime: no fractional seconds, explicit numeric offset (UTC as `+00:00`)."""
    as_utc = value.astimezone(UTC).replace(microsecond=0)
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def _tag(name: str) -> str:
    return f"{{{CAP_NS}}}{name}"


def _text(parent: etree._Element, name: str, value: object | None) -> None:
    if value is None:
        return
    child = etree.SubElement(parent, _tag(name))
    if isinstance(value, datetime):
        child.text = cap_datetime(value)
    elif isinstance(value, bool):
        child.text = "true" if value else "false"
    else:
        child.text = str(value)


def _key_values(parent: etree._Element, name: str, items: list[CAPKeyValue]) -> None:
    for item in items:
        node = etree.SubElement(parent, _tag(name))
        _text(node, "valueName", item.value_name)
        _text(node, "value", item.value)


def _resource(parent: etree._Element, resource: CAPResource) -> None:
    node = etree.SubElement(parent, _tag("resource"))
    _text(node, "resourceDesc", resource.resource_desc)
    _text(node, "mimeType", resource.mime_type)
    _text(node, "size", resource.size)
    _text(node, "uri", resource.uri)
    _text(node, "derefUri", resource.deref_uri)
    _text(node, "digest", resource.digest)


def _area(parent: etree._Element, area: CAPArea) -> None:
    node = etree.SubElement(parent, _tag("area"))
    _text(node, "areaDesc", area.area_desc)
    for polygon in area.polygon:
        _text(node, "polygon", polygon)
    for circle in area.circle:
        _text(node, "circle", circle)
    _key_values(node, "geocode", area.geocode)
    _text(node, "altitude", area.altitude)
    _text(node, "ceiling", area.ceiling)


def _info(parent: etree._Element, info: CAPInfo) -> None:
    node = etree.SubElement(parent, _tag("info"))
    _text(node, "language", info.language)
    for category in info.category:
        _text(node, "category", category)
    _text(node, "event", info.event)
    for response in info.response_type:
        _text(node, "responseType", response)
    _text(node, "urgency", info.urgency)
    _text(node, "severity", info.severity)
    _text(node, "certainty", info.certainty)
    _text(node, "audience", info.audience)
    _key_values(node, "eventCode", info.event_code)
    _text(node, "effective", info.effective)
    _text(node, "onset", info.onset)
    _text(node, "expires", info.expires)
    _text(node, "senderName", info.sender_name)
    _text(node, "headline", info.headline)
    _text(node, "description", info.description)
    _text(node, "instruction", info.instruction)
    _text(node, "web", info.web)
    _text(node, "contact", info.contact)
    _key_values(node, "parameter", info.parameter)
    for resource in info.resource:
        _resource(node, resource)
    for area in info.area:
        _area(node, area)


def to_element(message: CAPMessage) -> etree._Element:
    """Build the `alert` element tree."""
    alert = etree.fromstring(f'<alert xmlns="{CAP_NS}"/>'.encode())
    _text(alert, "identifier", message.identifier)
    _text(alert, "sender", message.sender)
    _text(alert, "sent", message.sent)
    _text(alert, "status", message.status)
    _text(alert, "msgType", message.msg_type)
    _text(alert, "source", message.source)
    _text(alert, "scope", message.scope)
    _text(alert, "restriction", message.restriction)
    _text(alert, "addresses", message.addresses)
    for code in message.code:
        _text(alert, "code", code)
    _text(alert, "note", message.note)
    _text(alert, "references", message.references)
    _text(alert, "incidents", message.incidents)
    for info in message.info:
        _info(alert, info)
    return alert


def render(message: CAPMessage) -> bytes:
    """Serialise `message` as UTF-8 CAP 1.2 XML with an XML declaration."""
    return etree.tostring(
        to_element(message), xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def with_xml(message: CAPMessage) -> CAPMessage:
    """Copy of `message` whose `xml` field holds its rendering."""
    return message.model_copy(update={"xml": render(message).decode("utf-8")})
