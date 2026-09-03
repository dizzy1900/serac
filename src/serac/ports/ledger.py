"""Port for the provenance ledger (`data/manifest.jsonl`)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus


class ManifestLedger(ABC):
    """Append-only record of every retrieval, request, or refusal."""

    @abstractmethod
    def append(self, entry: ManifestEntry) -> ManifestEntry:
        """Persist `entry` and return it."""

    @abstractmethod
    def entries(self) -> Iterator[ManifestEntry]:
        """Iterate all entries in insertion order."""

    def query(
        self,
        *,
        source: DataSource | None = None,
        aoi_id: str | None = None,
        event_id: str | None = None,
        status: ManifestStatus | None = None,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[ManifestEntry]:
        """Filter entries; a `window` matches entries whose time span overlaps it."""
        out: list[ManifestEntry] = []
        for entry in self.entries():
            if source is not None and entry.source != source:
                continue
            if aoi_id is not None and entry.aoi_id != aoi_id:
                continue
            if event_id is not None and entry.event_id != event_id:
                continue
            if status is not None and entry.status != status:
                continue
            if window is not None:
                if entry.time_start is None or entry.time_end is None:
                    continue
                if entry.time_end < window[0] or entry.time_start > window[1]:
                    continue
            out.append(entry)
        return out

    def latest(
        self, source: DataSource, product_id: str, aoi_id: str | None = None
    ) -> ManifestEntry | None:
        """Most recently recorded entry for a product, or None."""
        found = [
            e
            for e in self.entries()
            if e.source == source and e.product_id == product_id and e.aoi_id == aoi_id
        ]
        return max(found, key=lambda e: e.recorded_at) if found else None
