"""Sentinel-1 ASF adapter against the committed Chamoli Jan-Feb 2021 listing (53 granules)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from serac.adapters.eo._asf import bbox_wkt, feature_bbox, parse_asf_time
from serac.adapters.eo.asf_sentinel1 import (
    Sentinel1AsfAdapter,
    feature_to_record,
    group_by_relative_orbit,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.errors import CredentialsMissingError
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "chamoli-rishiganga"
BBOX = (79.68, 30.33, 79.80, 30.42)
LISTING = "asf/chamoli_s1_2021-01-01_2021-02-28.geojson"
T0 = datetime(2021, 1, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 28, 23, 59, 59, tzinfo=UTC)


def settings(**kw: Any) -> SeracSettings:
    return SeracSettings(_env_file=None, **kw)  # type: ignore[call-arg]


class FakeAsf:
    def __init__(self, features: list[dict[str, Any]]) -> None:
        self.features = features
        self.calls: list[dict[str, Any]] = []

    def geo_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return list(self.features)


class FakeDownloader:
    def __init__(self, payload: bytes = b"granule-bytes") -> None:
        self.payload = payload
        self.urls: list[str] = []

    def download(self, url: str, dest: Path) -> tuple[str, int]:
        import hashlib

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        self.urls.append(url)
        return hashlib.sha256(self.payload).hexdigest(), len(self.payload)


@pytest.fixture(scope="module")
def features(fixtures_dir: Path) -> list[dict[str, Any]]:
    doc = json.loads((fixtures_dir / LISTING).read_text("utf-8"))
    return list(doc["features"])


def request(**params: Any) -> IngestRequest:
    return IngestRequest(aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1, params=params)


def test_helpers(features: list[dict[str, Any]]) -> None:
    assert bbox_wkt(BBOX).startswith("POLYGON((79.68 30.33,79.8 30.33")
    assert parse_asf_time("2021-02-27T12:47:22Z") == datetime(2021, 2, 27, 12, 47, 22, tzinfo=UTC)
    assert parse_asf_time(None) is None
    bbox = feature_bbox(features[0])
    assert bbox is not None and bbox[0] < bbox[2] and bbox[1] < bbox[3]
    assert feature_bbox({"geometry": None}) is None


def test_feature_to_record_carries_bytes_and_orbit(features: list[dict[str, Any]]) -> None:
    r = feature_to_record(features[0], "lic", "https://example.invalid")
    assert r.source is DataSource.sentinel1_asf
    assert r.estimated_bytes == features[0]["properties"]["bytes"]
    assert r.properties["pathNumber"] == features[0]["properties"]["pathNumber"]
    assert r.url and r.url.startswith("https://datapool.asf.alaska.edu/")


def test_search_defaults_to_iw_slc_and_groups_by_orbit(features: list[dict[str, Any]]) -> None:
    fake = FakeAsf(features)
    adapter = Sentinel1AsfAdapter(fake, settings=settings(), git_sha=None)
    slc = adapter.search(request())
    assert len(slc) == 29 and all(p.product_level == "SLC" for p in slc)
    assert fake.calls[0]["processing_level"] == ["SLC"]
    assert fake.calls[0]["beam_mode"] == ["IW"]
    assert fake.calls[0]["intersects_with"] == bbox_wkt(BBOX)
    groups = group_by_relative_orbit(slc)
    assert sorted(groups) == [56, 63, 129, 165]
    assert [len(v) for v in groups.values()] == [7, 6, 6, 10]
    for scenes in groups.values():
        times = [s.time_start for s in scenes]
        assert times == sorted(times)  # type: ignore[type-var]


def test_search_filters(features: list[dict[str, Any]]) -> None:
    adapter = Sentinel1AsfAdapter(FakeAsf(features), settings=settings(), git_sha=None)
    grd = adapter.search(request(processing_level="GRD_HD"))
    assert len(grd) == 24 and all(p.product_level == "GRD_HD" for p in grd)
    desc = adapter.search(request(flight_direction="DESCENDING", relative_orbit=63))
    assert [p.properties["pathNumber"] for p in desc] == [63] * 6
    windowed = adapter.search(
        IngestRequest(
            aoi_id=AOI,
            bbox_4326=BBOX,
            time_start=datetime(2021, 2, 1, tzinfo=UTC),
            time_end=datetime(2021, 2, 15, tzinfo=UTC),
        )
    )
    assert all(
        datetime(2021, 2, 1, tzinfo=UTC) <= p.time_start <= datetime(2021, 2, 15, tzinfo=UTC)
        for p in windowed
        if p.time_start
    )
    with pytest.raises(ValueError, match="processing_level"):
        adapter.search(request(processing_level="RAW"))


def test_plan_sums_catalogue_bytes_and_lists_earthdata(features: list[dict[str, Any]]) -> None:
    adapter = Sentinel1AsfAdapter(FakeAsf(features), settings=settings(), git_sha=None)
    plan = adapter.plan(request(relative_orbit=63))
    expected = sum(
        f["properties"]["bytes"]
        for f in features
        if f["properties"]["pathNumber"] == 63 and f["properties"]["processingLevel"] == "SLC"
    )
    assert plan.estimated_bytes == expected
    assert "properties.bytes" in plan.estimate_basis
    assert [c.name for c in plan.requires_credentials] == ["Earthdata Login"]
    assert any("path 63: 6" in w for w in plan.warnings)
    assert plan.estimated_bytes and plan.estimated_bytes > 5 * 1024**3
    assert any("exceeds" in w for w in plan.warnings)


def test_fetch_without_credentials_records_not_fetched(
    features: list[dict[str, Any]], tmp_path: Path
) -> None:
    adapter = Sentinel1AsfAdapter(FakeAsf(features), settings=settings(), git_sha=None)
    plan = adapter.plan(request(relative_orbit=63))
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(CredentialsMissingError, match="Earthdata"):
        adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    rows = list(ledger.entries())
    assert len(rows) == 6 and {r.status for r in rows} == {ManifestStatus.not_fetched}
    assert all(r.provenance is Provenance.real and r.path is None for r in rows)
    assert not (tmp_path / "raw").exists()


def test_fetch_with_credentials_streams_and_hashes(
    features: list[dict[str, Any]], tmp_path: Path
) -> None:
    creds = settings(earthdata_username=SecretStr("u"), earthdata_password=SecretStr("p"))
    downloader = FakeDownloader()
    adapter = Sentinel1AsfAdapter(
        FakeAsf(features), downloader=downloader, settings=creds, repo_root=tmp_path, git_sha=None
    )
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI,
            bbox_4326=BBOX,
            time_start=datetime(2021, 1, 30, tzinfo=UTC),
            time_end=datetime(2021, 1, 30, 23, 59, tzinfo=UTC),
            params={"relative_orbit": 63},
        )
    )
    assert len(plan.products) == 1
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    entries = adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert len(entries) == 1
    e = entries[0]
    assert e.status is ManifestStatus.fetched and e.path is not None
    assert e.path.startswith("raw/sentinel1_asf/chamoli-rishiganga/S1A_IW_SLC__1SDV_20210130")
    assert (tmp_path / e.path).read_bytes() == b"granule-bytes"
    assert downloader.urls == [plan.products[0].url]
    assert e.params["pathNumber"] == 63
