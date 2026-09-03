"""NISAR constraints and adapter against the real 2026-09-03 probe (159 science granules)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from serac.adapters.eo.nisar import NisarAdapter, feature_to_record, level_counts
from serac.adapters.eo.nisar_constraints import (
    BETA_ACQUISITION_WINDOW,
    INSTRUMENT_GAP,
    PROVISIONAL_ACQUISITIONS_FROM,
    SCIENCE_LEVELS,
    MixedProductLevelError,
    NisarLevel,
    classify_collection,
    expected_level_for_acquisition,
    in_instrument_gap,
    is_science_product,
    overlaps_instrument_gap,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import ManifestStatus
from serac.errors import CredentialsMissingError, IngestRefusedError
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "lhende-khola-trishuli"
BBOX = (85.51, 28.27, 85.53, 28.29)
ALL_LEVELS = sorted(SCIENCE_LEVELS)


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
    def download(self, url: str, dest: Path) -> tuple[str, int]:
        import hashlib

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"h5")
        return hashlib.sha256(b"h5").hexdigest(), 2


@pytest.fixture(scope="module")
def probe(fixtures_dir: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((fixtures_dir / "asf/nisar_probe_2026-09-03.json").read_text())
    return doc


def ancillary(file_id: str, level: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "fileID": file_id,
            "sceneName": file_id,
            "collectionName": "NISAR_ANCILLARY",
            "processingLevel": level,
            "startTime": "2026-08-01T00:00:00Z",
            "url": "https://x.invalid/anc",
        },
    }


def test_classify_collection_rule() -> None:
    assert classify_collection("NISAR_L2_GCOV_BETA_V1", "X05009") is NisarLevel.beta
    assert classify_collection("NISAR_L3_SME2_PROVISIONAL_V1", "P05023") is NisarLevel.provisional
    assert classify_collection("NISAR_L1_RSLC_PROVISIONAL_V1") is NisarLevel.provisional
    # crid disagreeing with the collection name -> unknown, never a guess
    assert classify_collection("NISAR_L2_GCOV_BETA_V1", "P05023") is NisarLevel.unknown
    assert classify_collection("NISAR_L2_GCOV_V1") is NisarLevel.unknown
    assert classify_collection("NISAR_L0_RRSD_BETA_V1") is NisarLevel.unknown  # not a science level
    assert classify_collection(None) is NisarLevel.unknown
    assert classify_collection("") is NisarLevel.unknown


def test_constants_and_windows() -> None:
    assert (date(2025, 10, 1), date(2026, 1, 31)) == BETA_ACQUISITION_WINDOW
    assert date(2026, 6, 17) == PROVISIONAL_ACQUISITIONS_FROM
    assert (date(2026, 7, 27), date(2026, 8, 10)) == INSTRUMENT_GAP
    assert in_instrument_gap(datetime(2026, 8, 1, tzinfo=UTC))
    assert not in_instrument_gap(date(2026, 8, 11))
    assert overlaps_instrument_gap(
        datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 28, tzinfo=UTC)
    )
    assert not overlaps_instrument_gap(datetime(2026, 8, 11, tzinfo=UTC), None)
    assert overlaps_instrument_gap(None, None)
    assert expected_level_for_acquisition(date(2025, 12, 1)) is NisarLevel.beta
    assert expected_level_for_acquisition(date(2026, 6, 20)) is NisarLevel.provisional
    assert expected_level_for_acquisition(date(2026, 3, 1)) is NisarLevel.unknown
    assert is_science_product("GCOV", "NISAR_L2_PR_GCOV_...")
    assert not is_science_product("SCLKSCET", None)
    assert not is_science_product("GCOV", "NISAR_ECMWF_SMST_x")
    assert not is_science_product(None)


def test_probe_split_72_beta_87_provisional(probe: dict[str, Any]) -> None:
    """Every probe granule is `productionConfiguration: PR`; only collectionName separates them."""
    features = probe["features"]
    assert len(features) == 159
    assert {f["properties"]["productionConfiguration"] for f in features} == {"PR"}
    records = [feature_to_record(f) for f in features]
    counts = level_counts(records)
    assert counts == {"beta": 72, "provisional": 87}
    beta = [r for r in records if r.properties["nisar_level"] == "beta"]
    prov = [r for r in records if r.properties["nisar_level"] == "provisional"]
    assert {r.properties["crid"] for r in beta} == {"X05009", "X05010"}
    assert {r.properties["crid"] for r in prov} == {"P05023"}
    assert max(r.time_start for r in beta if r.time_start) <= datetime(2026, 1, 31, tzinfo=UTC)
    assert min(r.time_start for r in prov if r.time_start) >= datetime(2026, 6, 17, tzinfo=UTC)
    assert not any(in_instrument_gap(r.time_start) for r in records if r.time_start)
    for r in records:
        assert (
            r.time_start
            and expected_level_for_acquisition(r.time_start).value == r.properties["nisar_level"]
        )
    assert all(r.product_level in ("BETA", "PROVISIONAL") for r in records)
    assert all(r.estimated_bytes is None for r in records)  # `bytes` stripped from the probe


def test_search_filters_ancillary_and_levels(probe: dict[str, Any]) -> None:
    features = [
        *probe["features"],
        ancillary("NISAR_SCLKSCET_x", "SCLKSCET"),
        ancillary("NISAR_ECMWF_SMST_y", "GCOV"),
    ]
    fake = FakeAsf(features)
    adapter = NisarAdapter(fake, settings=settings(), git_sha=None)
    request = IngestRequest(aoi_id=AOI, bbox_4326=BBOX, params={"processing_level": ALL_LEVELS})
    found = adapter.search(request)
    assert len(found) == 159
    assert fake.calls[0]["platform"] == ["NISAR"]
    gcov = adapter.search(IngestRequest(aoi_id=AOI, bbox_4326=BBOX))  # default GCOV
    assert len(gcov) == 21 and {r.properties["processingLevel"] for r in gcov} == {"GCOV"}


def test_plan_refuses_mixing_unless_level_explicit(probe: dict[str, Any], tmp_path: Path) -> None:
    adapter = NisarAdapter(FakeAsf(probe["features"]), settings=settings(), git_sha=None)
    mixed = adapter.plan(
        IngestRequest(aoi_id=AOI, bbox_4326=BBOX, params={"processing_level": ALL_LEVELS})
    )
    assert mixed.refusals and "MixedProductLevelError" in mixed.refusals[0]
    assert not mixed.fetchable
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(IngestRefusedError, match="BETA and PROVISIONAL"):
        adapter.fetch(mixed, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert not ledger.path.exists()  # refusals write nothing

    prov = adapter.plan(
        IngestRequest(
            aoi_id=AOI,
            bbox_4326=BBOX,
            params={"processing_level": ALL_LEVELS, "level": "provisional"},
        )
    )
    assert not prov.refusals and len(prov.products) == 87
    assert all(p.product_level == "PROVISIONAL" for p in prov.products)
    assert prov.estimated_bytes is None and "unknown" in prov.estimate_basis
    beta = adapter.plan(
        IngestRequest(
            aoi_id=AOI, bbox_4326=BBOX, params={"processing_level": ALL_LEVELS, "level": "beta"}
        )
    )
    assert len(beta.products) == 72 and any("not inter-comparable" in w for w in beta.warnings)
    with pytest.raises(ValueError, match="unknown"):
        adapter.plan(IngestRequest(aoi_id=AOI, bbox_4326=BBOX, params={"level": "unknown"}))
    with pytest.raises(ValueError, match="beta"):
        adapter.plan(IngestRequest(aoi_id=AOI, bbox_4326=BBOX, params={"level": "validated"}))


def test_single_level_window_needs_no_flag_and_warns_on_gap(probe: dict[str, Any]) -> None:
    adapter = NisarAdapter(FakeAsf(probe["features"]), settings=settings(), git_sha=None)
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI,
            bbox_4326=BBOX,
            time_start=datetime(2026, 7, 1, tzinfo=UTC),
            time_end=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
        )
    )
    assert not plan.refusals and plan.products
    assert {p.product_level for p in plan.products} == {"PROVISIONAL"}
    assert any("instrument gap" in w for w in plan.warnings)
    assert not any(
        datetime(2026, 7, 27, tzinfo=UTC)
        <= p.time_start
        <= datetime(2026, 8, 10, 23, 59, tzinfo=UTC)
        for p in plan.products
        if p.time_start
    )


def test_unknown_level_is_always_refused(probe: dict[str, Any]) -> None:
    odd = json.loads(json.dumps(probe["features"][0]))
    odd["properties"]["collectionName"] = "NISAR_L2_GCOV_V1"
    odd["properties"]["fileID"] = "ODD"
    odd["properties"]["sceneName"] = "ODD"
    adapter = NisarAdapter(
        FakeAsf([odd, *probe["features"][:5]]), settings=settings(), git_sha=None
    )
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI,
            bbox_4326=BBOX,
            params={"processing_level": ALL_LEVELS, "level": "provisional"},
        )
    )
    assert any("unknown is always refused" in r for r in plan.refusals)
    assert "ODD" in plan.refusals[0]


def test_fetch_paths(probe: dict[str, Any], tmp_path: Path) -> None:
    request = IngestRequest(
        aoi_id=AOI,
        bbox_4326=BBOX,
        time_start=datetime(2026, 8, 28, tzinfo=UTC),
        time_end=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
        params={"level": "provisional"},
    )
    no_creds = NisarAdapter(FakeAsf(probe["features"]), settings=settings(), git_sha=None)
    plan = no_creds.plan(request)
    assert len(plan.products) == 2
    ledger = JsonlManifestLedger(tmp_path / "a.jsonl")
    with pytest.raises(CredentialsMissingError):
        no_creds.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    rows = list(ledger.entries())
    assert [r.status for r in rows] == [ManifestStatus.not_fetched] * 2
    assert {r.product_level for r in rows} == {"PROVISIONAL"}

    creds = settings(earthdata_username=SecretStr("u"), earthdata_password=SecretStr("p"))
    with_creds = NisarAdapter(
        FakeAsf(probe["features"]),
        downloader=FakeDownloader(),
        settings=creds,
        repo_root=tmp_path,
        git_sha=None,
    )
    plan = with_creds.plan(request)
    ledger = JsonlManifestLedger(tmp_path / "b.jsonl")
    entries = with_creds.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert [e.status for e in entries] == [ManifestStatus.fetched] * 2
    assert all(
        e.params["nisar_level"] == "provisional" and e.path and e.path.endswith(".h5")
        for e in entries
    )
    assert MixedProductLevelError.__mro__[1] is IngestRefusedError
