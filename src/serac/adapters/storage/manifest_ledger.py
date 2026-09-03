"""JSON Lines implementation of the provenance ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from serac.domain.manifest import ManifestEntry
from serac.ports.ledger import ManifestLedger


def sha256_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class JsonlManifestLedger(ManifestLedger):
    """Append-only `manifest.jsonl`; one `ManifestEntry` per line, never rewritten."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: ManifestEntry) -> ManifestEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json(exclude_none=True) + "\n")
        return entry

    def entries(self) -> Iterator[ManifestEntry]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    yield ManifestEntry.model_validate_json(text)
                except ValueError as exc:
                    raise ValueError(
                        f"{self.path}:{lineno}: invalid manifest entry: {exc}"
                    ) from exc
