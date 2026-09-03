from __future__ import annotations

from datetime import UTC, datetime

import pytest

from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance

NOW = datetime(2026, 9, 3, tzinfo=UTC)
SHA = "0" * 64


def _base(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "source": DataSource.dem_glo30,
        "product_id": "Copernicus_DSM_COG_10_N30_00_E079_00_DEM",
        "licence": "Copernicus DEM licence",
        "provenance": Provenance.real,
        "status": ManifestStatus.listed,
        "adapter": "test",
        "adapter_version": "0",
    }
    data.update(overrides)
    return data


def test_fetched_requires_path_hash_size_time() -> None:
    with pytest.raises(ValueError, match="status=fetched requires"):
        ManifestEntry(**_base(status=ManifestStatus.fetched))  # type: ignore[arg-type]
    entry = ManifestEntry(
        **_base(  # type: ignore[arg-type]
            status=ManifestStatus.fetched,
            path="data/fixtures/dem_glo30/x.tif",
            sha256=SHA,
            size_bytes=1,
            retrieved_at=NOW,
        )
    )
    assert entry.status is ManifestStatus.fetched


def test_synthetic_must_be_labelled_and_live_under_tests_fixtures() -> None:
    with pytest.raises(ValueError, match="provenance=synthetic"):
        ManifestEntry(**_base(status=ManifestStatus.synthetic))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tests/fixtures/synthetic"):
        ManifestEntry(
            **_base(  # type: ignore[arg-type]
                status=ManifestStatus.synthetic,
                provenance=Provenance.synthetic,
                path="data/fixtures/fake.tif",
                notes="placeholder",
            )
        )
    ok = ManifestEntry(
        **_base(  # type: ignore[arg-type]
            status=ManifestStatus.synthetic,
            provenance=Provenance.synthetic,
            path="tests/fixtures/synthetic/hyp3/pair_corr.tif",
            notes="32x32 placeholder; no Earthdata credentials in session",
        )
    )
    assert ok.provenance is Provenance.synthetic


def test_real_provenance_cannot_use_synthetic_status() -> None:
    with pytest.raises(ValueError):
        ManifestEntry(
            **_base(  # type: ignore[arg-type]
                status=ManifestStatus.synthetic, provenance=Provenance.real, notes="x"
            )
        )


def test_time_and_bbox_checks() -> None:
    with pytest.raises(ValueError, match="time_end"):
        ManifestEntry(**_base(time_start=NOW, time_end=datetime(2020, 1, 1, tzinfo=UTC)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bbox_4326"):
        ManifestEntry(**_base(bbox_4326=(10.0, 0.0, 5.0, 1.0)))  # type: ignore[arg-type]


def test_roundtrip_json() -> None:
    entry = ManifestEntry(**_base())  # type: ignore[arg-type]
    again = ManifestEntry.model_validate_json(entry.model_dump_json())
    assert again == entry
