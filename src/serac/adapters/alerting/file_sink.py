"""Write CAP messages to a directory (and optionally to the log). Sends nothing anywhere.

One file per message, named by its CAP identifier, plus an append-only `index.jsonl` so a
replay or a validation suite can list what was emitted without re-parsing the XML.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from serac.alerting.signing import is_signed, signature_key_name
from serac.domain.cap import CAPMessage
from serac.ports.alert_sink import AlertDelivery, AlertSink, AlertSinkError

INDEX_FILENAME = "index.jsonl"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

logger = logging.getLogger(__name__)


def safe_filename(identifier: str) -> str:
    """A CAP identifier as a filesystem-safe name; refuses one that reduces to nothing."""
    cleaned = _UNSAFE.sub("_", identifier).strip("._")
    if not cleaned:
        raise AlertSinkError(f"identifier {identifier!r} has no filesystem-safe form")
    return cleaned[:180] + ".cap.xml"


class FileAlertSink(AlertSink):
    """Persist each message under `directory`. The default sink, and the only safe one."""

    name = "file"

    def __init__(self, directory: Path, *, log: bool = False, overwrite: bool = True) -> None:
        self.directory = directory
        self.log = log
        self.overwrite = overwrite
        self.written: list[Path] = []

    def deliver(self, message: CAPMessage) -> AlertDelivery:
        attempted = datetime.now(tz=UTC)
        if not message.xml:
            return AlertDelivery(
                sink=self.name,
                identifier=message.identifier,
                delivered=False,
                target=None,
                attempted_utc=attempted,
                detail="message carries no rendered XML; render and validate it first",
            )
        path = self.directory / safe_filename(message.identifier)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if path.exists() and not self.overwrite:
                return AlertDelivery(
                    sink=self.name,
                    identifier=message.identifier,
                    delivered=False,
                    target=str(path),
                    attempted_utc=attempted,
                    detail=f"{path} exists and overwrite=False",
                    signed=is_signed(message.xml),
                )
            path.write_text(message.xml, encoding="utf-8")
            self._append_index(message, path, attempted)
        except OSError as exc:
            return AlertDelivery(
                sink=self.name,
                identifier=message.identifier,
                delivered=False,
                target=str(path),
                attempted_utc=attempted,
                detail=f"{type(exc).__name__}: {exc}",
            )
        self.written.append(path)
        signed = is_signed(message.xml)
        if self.log:
            logger.info(
                "CAP %s status=%s scope=%s signed=%s -> %s",
                message.identifier,
                message.status,
                message.scope,
                signed,
                path,
            )
        return AlertDelivery(
            sink=self.name,
            identifier=message.identifier,
            delivered=True,
            target=str(path),
            attempted_utc=attempted,
            detail=f"wrote {path.stat().st_size} bytes",
            signed=signed,
        )

    def _append_index(self, message: CAPMessage, path: Path, attempted: datetime) -> None:
        row = {
            "identifier": message.identifier,
            "sender": message.sender,
            "sent": message.sent.isoformat(),
            "status": message.status,
            "msg_type": message.msg_type,
            "scope": message.scope,
            "path": path.name,
            "written_utc": attempted.isoformat(),
            "signed": is_signed(message.xml or ""),
            "key_name": signature_key_name(message.xml or ""),
        }
        with (self.directory / INDEX_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
