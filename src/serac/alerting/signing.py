"""Enveloped XML-Signature over a CAP 1.2 alert, using Ed25519.

Why a signature belongs inside the document rather than beside it: a CAP message is passed
from hand to hand -- bus, file, HTTP POST, someone's mailbox -- and a detached signature is
lost at the first hop. The CAP 1.2 schema anticipates this. Its `alert` sequence ends with

    <any namespace="http://www.w3.org/2000/09/xmldsig#" processContents="lax"/>

so a `ds:Signature` appended as the **last child of `alert`** is schema-valid, and
`contracts/vendor/cap/CAP-v1.2.xsd` compiles with no resolver and no network because that
wildcard is `lax`. `verify_cap_signature` therefore round-trips through the same
`CapValidator` the unsigned message went through.

What is signed
--------------
* `Reference URI=""` -- the whole document, with the enveloped-signature transform removing
  the `ds:Signature` subtree before digesting, then exclusive C14N. Digest is SHA-256.
* `SignatureValue` is Ed25519 over the exclusive C14N of `ds:SignedInfo`.
* `SignatureMethod` is `http://www.w3.org/2021/04/xmldsig-more#eddsa-ed25519` (RFC 9231).

`ds:KeyInfo` carries a `ds:KeyName` of `ed25519:sha256:<hex>` -- the fingerprint of the public
key, not the key. Verification takes the public key as an argument, so key distribution is
deliberately out of band: a message that carried its own verification key would authenticate
nothing.

Limits, stated because they matter operationally: there is no certificate chain, no revocation
and no timestamp authority. This proves that the holder of one private key produced these
bytes. It does not prove who that holder is, and serac ships no trust store.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from lxml import etree

from serac.alerting.keys import public_key_fingerprint
from serac.domain.cap import CAPMessage
from serac.errors import SeracError

DS_NS = "http://www.w3.org/2000/09/xmldsig#"
CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

C14N_EXCLUSIVE = "http://www.w3.org/2001/10/xml-exc-c14n#"
ENVELOPED_SIGNATURE = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
SHA256_DIGEST = "http://www.w3.org/2001/04/xmlenc#sha256"
ED25519_SIGNATURE = "http://www.w3.org/2021/04/xmldsig-more#eddsa-ed25519"
"""RFC 9231 identifier for Ed25519 in XML Security."""

SIGNATURE_PROFILE = "serac-cap-ed25519-enveloped-v1"


class CapSignatureError(SeracError):
    """A CAP document could not be signed, or its signature could not be checked."""


def _ds(name: str) -> str:
    return f"{{{DS_NS}}}{name}"


def _c14n(element: etree._Element) -> bytes:
    """Exclusive canonical XML of one element, as XMLDSig requires."""
    return etree.tostring(element, method="c14n", exclusive=True, with_comments=False)


def _parse(xml: bytes | str) -> etree._Element:
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    parser = etree.XMLParser(no_network=True, resolve_entities=False)
    try:
        return etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise CapSignatureError(f"not well-formed XML: {exc}") from exc


def _signature_of(alert: etree._Element) -> etree._Element | None:
    found = alert.findall(_ds("Signature"))
    return found[-1] if found else None


def _build_signed_info(digest_b64: str) -> etree._Element:
    signed_info = etree.Element(_ds("SignedInfo"), nsmap={"ds": DS_NS})
    etree.SubElement(signed_info, _ds("CanonicalizationMethod"), Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(signed_info, _ds("SignatureMethod"), Algorithm=ED25519_SIGNATURE)
    reference = etree.SubElement(signed_info, _ds("Reference"), URI="")
    transforms = etree.SubElement(reference, _ds("Transforms"))
    etree.SubElement(transforms, _ds("Transform"), Algorithm=ENVELOPED_SIGNATURE)
    etree.SubElement(transforms, _ds("Transform"), Algorithm=C14N_EXCLUSIVE)
    etree.SubElement(reference, _ds("DigestMethod"), Algorithm=SHA256_DIGEST)
    etree.SubElement(reference, _ds("DigestValue")).text = digest_b64
    return signed_info


def sign_cap_xml(xml: bytes | str, private_key: Ed25519PrivateKey) -> bytes:
    """Return `xml` with a `ds:Signature` appended as the last child of `alert`.

    Refuses a document that already carries one: re-signing in place would silently replace
    somebody else's attestation.
    """
    alert = _parse(xml)
    if alert.tag != f"{{{CAP_NS}}}alert":
        raise CapSignatureError(f"root element is {alert.tag!r}, expected a CAP 1.2 alert")
    if _signature_of(alert) is not None:
        raise CapSignatureError("document already carries a ds:Signature; refusing to re-sign")

    digest = hashlib.sha256(_c14n(alert)).digest()
    signed_info = _build_signed_info(base64.b64encode(digest).decode("ascii"))

    signature = etree.Element(_ds("Signature"), nsmap={"ds": DS_NS})
    signature.append(signed_info)
    value = private_key.sign(_c14n(signed_info))
    etree.SubElement(signature, _ds("SignatureValue")).text = base64.b64encode(value).decode(
        "ascii"
    )
    key_info = etree.SubElement(signature, _ds("KeyInfo"))
    etree.SubElement(
        key_info, _ds("KeyName")
    ).text = f"ed25519:{public_key_fingerprint(private_key.public_key())}"
    alert.append(signature)
    return etree.tostring(alert, xml_declaration=True, encoding="UTF-8", pretty_print=True)


@dataclass(frozen=True)
class SignatureCheck:
    """The outcome of checking one signature. `valid` is true only when everything held."""

    valid: bool
    reason: str
    key_name: str | None = None
    signature_method: str | None = None

    def __bool__(self) -> bool:
        return self.valid


def signature_key_name(xml: bytes | str) -> str | None:
    """The `ds:KeyName` a signed document claims, or None when it is unsigned."""
    signature = _signature_of(_parse(xml))
    if signature is None:
        return None
    node = signature.find(f"{_ds('KeyInfo')}/{_ds('KeyName')}")
    return None if node is None else (node.text or None)


def is_signed(xml: bytes | str) -> bool:
    """Whether a `ds:Signature` is present. Says nothing about whether it verifies."""
    return _signature_of(_parse(xml)) is not None


def verify_cap_signature(xml: bytes | str, public_key: Ed25519PublicKey) -> SignatureCheck:
    """Check the enveloped signature against `public_key`. Never raises on a bad signature."""
    try:
        alert = _parse(xml)
    except CapSignatureError as exc:
        return SignatureCheck(False, str(exc))
    signature = _signature_of(alert)
    if signature is None:
        return SignatureCheck(False, "no ds:Signature element")

    signed_info = signature.find(_ds("SignedInfo"))
    value_node = signature.find(_ds("SignatureValue"))
    if signed_info is None or value_node is None or not (value_node.text or "").strip():
        return SignatureCheck(False, "ds:Signature is missing SignedInfo or SignatureValue")

    method_node = signed_info.find(_ds("SignatureMethod"))
    method = None if method_node is None else method_node.get("Algorithm")
    if method != ED25519_SIGNATURE:
        return SignatureCheck(
            False, f"unsupported SignatureMethod {method!r}", signature_method=method
        )

    key_name_node = signature.find(f"{_ds('KeyInfo')}/{_ds('KeyName')}")
    key_name = None if key_name_node is None else key_name_node.text

    digest_node = signed_info.find(f"{_ds('Reference')}/{_ds('DigestValue')}")
    if digest_node is None or not (digest_node.text or "").strip():
        return SignatureCheck(False, "Reference carries no DigestValue", key_name, method)

    # The enveloped transform: digest the document with the signature removed. Detaching
    # mutates the tree we parsed, which is a local copy, so nothing outside this call sees it.
    parent = signature.getparent()
    if parent is None:
        return SignatureCheck(False, "ds:Signature has no parent element", key_name, method)
    signed_info_c14n = _c14n(signed_info)
    parent.remove(signature)
    recomputed = base64.b64encode(hashlib.sha256(_c14n(alert)).digest()).decode("ascii")
    if recomputed != (digest_node.text or "").strip():
        return SignatureCheck(
            False, "digest mismatch: the alert changed after it was signed", key_name, method
        )

    try:
        raw_signature = base64.b64decode((value_node.text or "").strip(), validate=True)
    except (ValueError, TypeError):
        return SignatureCheck(False, "SignatureValue is not valid base64", key_name, method)
    try:
        public_key.verify(raw_signature, signed_info_c14n)
    except InvalidSignature:
        return SignatureCheck(
            False, "signature does not verify against the supplied public key", key_name, method
        )
    expected = f"ed25519:{public_key_fingerprint(public_key)}"
    if key_name is not None and key_name != expected:
        return SignatureCheck(
            False,
            f"KeyName {key_name!r} does not name the key that verified it ({expected})",
            key_name,
            method,
        )
    return SignatureCheck(True, f"verified against {expected}", key_name, method)


def sign_message(message: CAPMessage, private_key: Ed25519PrivateKey) -> CAPMessage:
    """Copy of `message` whose `xml` field holds the signed rendering.

    Requires `message.xml` to be populated already: signing a re-rendering would sign bytes
    nobody validated.
    """
    if not message.xml:
        raise CapSignatureError(
            f"{message.identifier}: no rendered XML to sign; render and XSD-validate first"
        )
    signed = sign_cap_xml(message.xml, private_key)
    return message.model_copy(update={"xml": signed.decode("utf-8")})
