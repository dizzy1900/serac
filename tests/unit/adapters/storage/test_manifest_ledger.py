from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance


def _entry(product_id: str, **kw: object) -> ManifestEntry:
    base: dict[str, object] = {
        "source": DataSource.usgs_comcat,
        "product_id": product_id,
        "licence": "US-PD",
        "provenance": Provenance.real,
        "status": ManifestStatus.listed,
        "adapter": "test",
        "adapter_version": "0",
    }
    base.update(kw)
    return ManifestEntry(**base)  # type: ignore[arg-type]


def test_append_and_read_back(tmp_path: Path) -> None:
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    assert list(ledger.entries()) == []
    a = ledger.append(_entry("a"))
    b = ledger.append(_entry("b", aoi_id="chamoli-rishiganga"))
    assert [e.entry_id for e in ledger.entries()] == [a.entry_id, b.entry_id]
    assert ledger.query(aoi_id="chamoli-rishiganga") == [b]
    assert ledger.latest(DataSource.usgs_comcat, "a") == a
    assert ledger.latest(DataSource.usgs_comcat, "zzz") is None


def test_window_query(tmp_path: Path) -> None:
    ledger = JsonlManifestLedger(tmp_path / "m.jsonl")
    t0 = datetime(2021, 2, 1, tzinfo=UTC)
    t1 = datetime(2021, 2, 10, tzinfo=UTC)
    ledger.append(_entry("in", time_start=t0, time_end=t1))
    ledger.append(
        _entry(
            "out",
            time_start=datetime(2022, 1, 1, tzinfo=UTC),
            time_end=datetime(2022, 1, 2, tzinfo=UTC),
        )
    )
    ledger.append(_entry("untimed"))
    hits = ledger.query(window=(datetime(2021, 2, 5, tzinfo=UTC), datetime(2021, 3, 1, tzinfo=UTC)))
    assert [e.product_id for e in hits] == ["in"]


def test_corrupt_line_is_reported_with_position(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    path.write_text('{"not": "an entry"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"m\.jsonl:1"):
        list(JsonlManifestLedger(path).entries())


def test_sha256_of_file(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"serac")
    assert sha256_of_file(p) == "938fffe8016ce4ef82a6db1fe58746004c70437da3b0ecec2113f72e57d53d07"
