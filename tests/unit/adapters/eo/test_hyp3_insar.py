"""HyP3 InSAR: pair planning on the real listing, job lifecycle with a fake HyP3, synthetic pair."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from pydantic import SecretStr

from serac.adapters.eo.asf_sentinel1 import feature_to_record
from serac.adapters.eo.hyp3_insar import (
    Hyp3InsarAdapter,
    Hyp3JobInfo,
    InSARPairPlanner,
    JobsLedger,
    jobs_ledger_path,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.errors import CredentialsMissingError
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "chamoli-rishiganga"
BBOX = (79.68, 30.33, 79.80, 30.42)
T0 = datetime(2021, 1, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 28, 23, 59, 59, tzinfo=UTC)
SYNTHETIC_PAIR = "S1_063_20210130_20210211"


def settings(**kw: Any) -> SeracSettings:
    return SeracSettings(_env_file=None, **kw)  # type: ignore[call-arg]


class FakeAsf:
    def __init__(self, features: list[dict[str, Any]]) -> None:
        self.features = features

    def geo_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.features)


class FakeHyp3:
    """Scripted lifecycle: submit -> PENDING; each get_job advances one step."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.submitted: list[tuple[str, str, str]] = []
        self.state: dict[str, list[str]] = {}
        self.fail = fail or set()

    def submit_insar_job(
        self, reference: str, secondary: str, *, name: str, looks: str
    ) -> Hyp3JobInfo:
        job_id = f"job-{len(self.submitted) + 1}"
        self.submitted.append((reference, secondary, name))
        final = "FAILED" if name.rsplit("-", 1)[-1] in self.fail else "SUCCEEDED"
        self.state[job_id] = ["PENDING", "RUNNING", final]
        return Hyp3JobInfo(job_id=job_id, status="PENDING", name=name)

    def get_job(self, job_id: str) -> Hyp3JobInfo:
        steps = self.state[job_id]
        if len(steps) > 1:
            steps.pop(0)
        status = steps[0]
        return Hyp3JobInfo(
            job_id=job_id,
            status=status,
            files=({"filename": "x_corr.tif", "url": f"https://hyp3.invalid/{job_id}/x_corr.tif"},),
        )

    def watch(self, job_id: str, *, timeout_s: float) -> Hyp3JobInfo:
        self.state[job_id] = self.state[job_id][-1:]
        return self.get_job(job_id)

    def download(self, job_id: str, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        paths = [dest / "x_corr.tif", dest / "x_los_disp.tif"]
        for p in paths:
            p.write_bytes(f"{job_id}:{p.name}".encode())
        return paths


@pytest.fixture(scope="module")
def features(fixtures_dir: Path) -> list[dict[str, Any]]:
    doc = json.loads((fixtures_dir / "asf/chamoli_s1_2021-01-01_2021-02-28.geojson").read_text())
    return list(doc["features"])


def test_pair_planner_same_track_frame_and_dt(features: list[dict[str, Any]]) -> None:
    records = [feature_to_record(f, "lic", "https://x.invalid") for f in features]
    pairs = InSARPairPlanner().plan_pairs(records)
    ids = [p.pair_id for p in pairs]
    assert SYNTHETIC_PAIR in ids
    assert all(0 < p.dt_days <= 12 for p in pairs)
    for p in pairs:
        assert p.reference.properties["pathNumber"] == p.secondary.properties["pathNumber"]
        assert p.reference.properties["frameNumber"] == p.secondary.properties["frameNumber"]
        assert p.reference.product_level == "SLC" == p.secondary.product_level
    # Path 63: 5 S1A frame-492 scenes 12 d apart -> 4 pairs; the S1B frame-491 scene of
    # 2021-02-17 pairs with nothing at tolerance 0 but with 02-11 and 02-23 (6 d) at tolerance 1.
    p63 = [p for p in pairs if p.path_number == 63]
    assert len(p63) == 4
    loose = InSARPairPlanner(frame_tolerance=1).plan_pairs(records)
    assert len([p for p in loose if p.path_number == 63]) == 6
    assert all(p.dt_days <= 6 for p in InSARPairPlanner(max_days=6).plan_pairs(records))
    with pytest.raises(ValueError):
        InSARPairPlanner(max_days=0)


def test_plan_has_unknown_size_and_needs_earthdata(features: list[dict[str, Any]]) -> None:
    adapter = Hyp3InsarAdapter(
        FakeAsf(features), hyp3=FakeHyp3(), settings=settings(), git_sha=None
    )
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1, params={"relative_orbit": 63}
        )
    )
    assert plan.estimated_bytes is None
    assert "does not publish product sizes" in plan.estimate_basis
    assert [p.product_id for p in plan.products] == [
        "S1_063_20210106_20210118",
        "S1_063_20210118_20210130",
        "S1_063_20210130_20210211",
        "S1_063_20210211_20210223",
    ]
    assert [c.name for c in plan.requires_credentials] == ["Earthdata Login"]
    assert any("confirmation" in w for w in plan.warnings)


def test_fetch_without_credentials_records_not_fetched(
    features: list[dict[str, Any]], tmp_path: Path
) -> None:
    adapter = Hyp3InsarAdapter(
        FakeAsf(features), hyp3=FakeHyp3(), settings=settings(), git_sha=None
    )
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1, params={"relative_orbit": 63}
        )
    )
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(CredentialsMissingError):
        adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert {e.status for e in ledger.entries()} == {ManifestStatus.not_fetched}
    assert not jobs_ledger_path(tmp_path, AOI).exists()


def test_job_lifecycle_requested_then_fetched(
    features: list[dict[str, Any]], tmp_path: Path
) -> None:
    creds = settings(earthdata_username=SecretStr("u"), earthdata_password=SecretStr("p"))
    hyp3 = FakeHyp3(fail={"S1_063_20210118_20210130"})
    adapter = Hyp3InsarAdapter(
        FakeAsf(features), hyp3=hyp3, settings=creds, repo_root=tmp_path, git_sha=None
    )
    req = IngestRequest(
        aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1, params={"relative_orbit": 63}
    )
    plan = adapter.plan(req)
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")

    # 1. submit: four jobs, four `requested` rows, jobs ledger written
    first = adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert [e.status for e in first] == [ManifestStatus.requested] * 4
    assert all(e.params["job_id"].startswith("job-") for e in first)
    assert [s[2] for s in hyp3.submitted] == [f"serac-{AOI}-{p.product_id}" for p in plan.products]
    jobs = JobsLedger(jobs_ledger_path(tmp_path, AOI))
    assert {r.status for r in jobs.latest_by_pair().values()} == {"PENDING"}

    # 2. poll while running: nothing new in the provenance ledger, jobs ledger advances
    assert adapter.poll(plan, dest_root=tmp_path, ledger=ledger) == []
    assert {r.status for r in jobs.latest_by_pair().values()} == {"RUNNING"}
    assert len(list(ledger.entries())) == 4

    # 3. poll when done: three succeed (files hashed, `fetched`), one fails (`failed`)
    done = adapter.poll(plan, dest_root=tmp_path, ledger=ledger)
    statuses = sorted(e.status.value for e in done)
    assert statuses == ["failed", *(["fetched"] * 6)]
    fetched = [e for e in done if e.status is ManifestStatus.fetched]
    for e in fetched:
        assert e.path and (tmp_path / e.path).exists()
        assert e.sha256 and e.params["job_id"]
        assert e.product_level == "INSAR_GAMMA"
    by_pair = jobs.latest_by_pair()
    assert by_pair["S1_063_20210118_20210130"].status == "FAILED"
    assert len(by_pair["S1_063_20210130_20210211"].files) == 2

    # 4. polling again downloads nothing twice
    assert adapter.poll(plan, dest_root=tmp_path, ledger=ledger) == []


def test_fetch_with_wait_completes_in_one_call(
    features: list[dict[str, Any]], tmp_path: Path
) -> None:
    creds = settings(earthdata_username=SecretStr("u"), earthdata_password=SecretStr("p"))
    adapter = Hyp3InsarAdapter(
        FakeAsf(features), hyp3=FakeHyp3(), settings=creds, repo_root=tmp_path, git_sha=None
    )
    req = IngestRequest(
        aoi_id=AOI,
        bbox_4326=BBOX,
        time_start=datetime(2021, 1, 29, tzinfo=UTC),
        time_end=datetime(2021, 2, 12, tzinfo=UTC),
        params={"relative_orbit": 63, "wait": True},
    )
    plan = adapter.plan(req)
    assert [p.product_id for p in plan.products] == [SYNTHETIC_PAIR]
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    entries = adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert [e.status.value for e in entries] == ["fetched", "fetched"]
    assert [e.status.value for e in ledger.entries()] == ["requested", "fetched", "fetched"]


def test_synthetic_pair_is_labelled_everywhere(repo_root: Path, synthetic_dir: Path) -> None:
    """The committed placeholder: tagged in the GeoTIFFs and recorded `synthetic` in the ledger."""
    pair_dir = synthetic_dir / "hyp3" / AOI / SYNTHETIC_PAIR
    files = sorted(pair_dir.glob("*.tif"))
    assert [f.name for f in files] == [
        f"{SYNTHETIC_PAIR}_corr.tif",
        f"{SYNTHETIC_PAIR}_los_disp.tif",
    ]
    for f in files:
        with rasterio.open(f) as ds:
            assert ds.tags()["SERAC_PROVENANCE"] == "synthetic"
            assert ds.crs.to_epsg() == 32644 and ds.shape == (32, 32)
            assert ds.res == (80.0, 80.0)
            arr = ds.read(1)
            assert np.isfinite(arr).all()
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    # Scoped to the synthetic placeholder rows. This used to assert that *every* hyp3_insar row
    # in the ledger was synthetic, which held only while no real InSAR had ever been ingested;
    # M3's burst-InSAR network now writes real rows under the same DataSource. The invariant
    # that matters is the one below: synthetic rows are labelled synthetic and live only under
    # tests/fixtures/synthetic/, and no real row is ever recorded there.
    rows = [
        e
        for e in ledger.entries()
        if e.source is DataSource.hyp3_insar and e.provenance is Provenance.synthetic
    ]
    assert len(rows) >= 2
    for e in rows:
        assert e.status is ManifestStatus.synthetic
        assert e.path and e.path.startswith("tests/fixtures/synthetic/hyp3/")
        assert e.notes and "no Earthdata credentials" in e.notes
        assert e.sha256 and (repo_root / e.path).exists()
    for e in ledger.entries():
        if e.path and e.path.startswith("tests/fixtures/synthetic/"):
            assert e.provenance is Provenance.synthetic
