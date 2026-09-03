"""The vendored CAP 1.2 XSD accepts a valid stub message and rejects mutated ones, offline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from lxml import etree

from serac.adapters.cap.cap12 import CAP_NS, render
from serac.domain.detection import DetectionCandidate
from serac.domain.seismic import Sncl
from serac.streaming.cap_stub import cap_message_for
from serac.validation.cap import CapValidationError, CapValidator, verify_vendor_manifest

T0 = datetime(2026, 8, 26, 2, 53, tzinfo=UTC)


@pytest.fixture(scope="module")
def cap_dir(repo_root: Path) -> Path:
    return repo_root / "contracts" / "vendor" / "cap"


@pytest.fixture(scope="module")
def validator(cap_dir: Path) -> CapValidator:
    return CapValidator(cap_dir / "CAP-v1.2.xsd")


@pytest.fixture(scope="module")
def valid_xml() -> bytes:
    det = DetectionCandidate(
        detection_id="d1",
        sncl=Sncl(network="NK", station="KKN", location="", channel="BHZ"),
        detector="lp-sp-ratio-stub",
        detector_version="0.1.0",
        window_start_utc=T0,
        window_end_utc=T0,
        detected_at_stream_utc=T0,
        score=1.0,
        threshold=0.5,
    )
    return render(cap_message_for(det, sent=T0))


def test_xsd_uses_lax_any_for_xmldsig_so_no_resolver_is_needed(cap_dir: Path) -> None:
    xsd = (cap_dir / "CAP-v1.2.xsd").read_text(encoding="utf-8")
    assert "xmldsig-core-schema.xsd" not in xsd  # no import to resolve
    assert 'namespace = "http://www.w3.org/2000/09/xmldsig#"' in xsd
    assert 'processContents = "lax"' in xsd


def test_vendor_manifest_checksums_match(cap_dir: Path) -> None:
    assert verify_vendor_manifest(cap_dir) == []


def test_valid_message_passes(validator: CapValidator, valid_xml: bytes) -> None:
    assert validator.errors(valid_xml) == []
    validator.validate(valid_xml)


def _mutate(xml: bytes, path: str, text: str | None) -> bytes:
    root = etree.fromstring(xml)
    node = root.find(path.replace("cap:", f"{{{CAP_NS}}}"))
    assert node is not None
    if text is None:
        parent = node.getparent()
        assert parent is not None
        parent.remove(node)
    else:
        node.text = text
    return etree.tostring(root)


@pytest.mark.parametrize(
    ("path", "text", "expect"),
    [
        ("cap:status", "Bogus", "Bogus"),
        ("cap:sent", "2026-08-26T02:53:00Z", "sent"),  # CAP forbids the Z suffix
        ("cap:info/cap:urgency", "Soon", "Soon"),
        ("cap:identifier", None, "identifier"),
        ("cap:info/cap:category", None, "category"),
    ],
)
def test_mutated_messages_fail(
    validator: CapValidator, valid_xml: bytes, path: str, text: str | None, expect: str
) -> None:
    bad = _mutate(valid_xml, path, text)
    errors = validator.errors(bad)
    assert errors, "mutation should fail validation"
    assert any(expect in e for e in errors)
    with pytest.raises(CapValidationError):
        validator.validate(bad)


def test_reordered_elements_fail(validator: CapValidator, valid_xml: bytes) -> None:
    root = etree.fromstring(valid_xml)
    first = root[0]
    root.remove(first)
    root.append(first)  # identifier moved to the end
    assert validator.errors(etree.tostring(root))


def test_garbage_and_wrong_root_fail(validator: CapValidator) -> None:
    assert validator.errors(b"<not xml") == ["not well-formed XML"]
    assert "expected" in validator.errors(b"<alert/>")[0]
