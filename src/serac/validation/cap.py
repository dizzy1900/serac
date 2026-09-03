"""Offline CAP 1.2 validation against the vendored OASIS schema.

`contracts/vendor/cap/CAP-v1.2.xsd` does not import the XML-Signature schema: it declares
`<any namespace="http://www.w3.org/2000/09/xmldsig#" processContents="lax"/>`, so lxml
compiles it with no resolver and no network. `xmldsig-core-schema.xsd` is vendored alongside
for a future signing stage. `verify_vendor_manifest` re-hashes both files against
`MANIFEST.json` so a silently edited schema cannot pass a message it should not.
"""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from lxml import etree

from serac.adapters.storage.manifest_ledger import sha256_of_file
from serac.errors import SeracError

CAP_XSD_RELATIVE = Path("contracts") / "vendor" / "cap" / "CAP-v1.2.xsd"
CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"


def default_xsd_path(repo_root: Path | None = None) -> Path:
    root = repo_root if repo_root is not None else Path.cwd()
    return root / CAP_XSD_RELATIVE


class CapValidationError(SeracError):
    """A CAP document did not validate against the CAP 1.2 XSD."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "invalid CAP document")
        self.errors = errors


class CapValidator:
    """Compiled CAP 1.2 XML Schema; `errors()` lists problems, `validate()` raises on any."""

    def __init__(self, xsd_path: Path | None = None) -> None:
        self.xsd_path = xsd_path if xsd_path is not None else default_xsd_path()
        if not self.xsd_path.exists():
            raise FileNotFoundError(f"CAP schema not found: {self.xsd_path}")

    @cached_property
    def schema(self) -> etree.XMLSchema:
        parser = etree.XMLParser(no_network=True, resolve_entities=False)
        doc = etree.parse(str(self.xsd_path), parser)
        return etree.XMLSchema(doc)

    @staticmethod
    def _parse(xml: bytes | str) -> etree._Element | None:
        raw = xml.encode("utf-8") if isinstance(xml, str) else xml
        parser = etree.XMLParser(no_network=True, resolve_entities=False)
        try:
            return etree.fromstring(raw, parser)
        except etree.XMLSyntaxError:
            return None

    def errors(self, xml: bytes | str) -> list[str]:
        """Human-readable schema violations; empty when the document validates."""
        element = self._parse(xml)
        if element is None:
            return ["not well-formed XML"]
        if element.tag != f"{{{CAP_NS}}}alert":
            return [f"root element is {element.tag!r}, expected {{{CAP_NS}}}alert"]
        if self.schema.validate(element):
            return []
        # lxml-stubs type the error log opaquely; its str() is one entry per line.
        return [line for line in str(self.schema.error_log).splitlines() if line.strip()]

    def is_valid(self, xml: bytes | str) -> bool:
        return not self.errors(xml)

    def validate(self, xml: bytes | str) -> None:
        problems = self.errors(xml)
        if problems:
            raise CapValidationError(problems)


def verify_vendor_manifest(cap_dir: Path) -> list[str]:
    """Problems with `MANIFEST.json` versus the files on disk (empty when everything matches)."""
    manifest_path = cap_dir / "MANIFEST.json"
    if not manifest_path.exists():
        return [f"{manifest_path} missing"]
    doc: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    files = doc.get("files")
    if not isinstance(files, list) or not files:
        return [f"{manifest_path}: no files listed"]
    for meta in files:
        name = meta.get("file")
        path = cap_dir / str(name)
        if meta.get("status") != "fetched":
            problems.append(f"{name}: status is {meta.get('status')!r}, not fetched")
            continue
        if not path.exists():
            problems.append(f"{name}: listed but missing")
            continue
        if sha256_of_file(path) != meta.get("sha256"):
            problems.append(f"{name}: sha256 drifted from MANIFEST.json")
        if path.stat().st_size != meta.get("size_bytes"):
            problems.append(f"{name}: size drifted from MANIFEST.json")
    if not any(m.get("file") == "CAP-v1.2.xsd" for m in files):
        problems.append("CAP-v1.2.xsd not listed in MANIFEST.json")
    return problems
