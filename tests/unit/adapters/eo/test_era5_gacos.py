"""ERA5 (cdsapi behind a fake) and GACOS (request / poll / receive with fake HTTP)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from serac.adapters.eo.era5_cds import (
    ERA5_DATASET,
    Era5Adapter,
    build_cds_request,
    era5_area,
    era5_cells,
)
from serac.adapters.eo.gacos import ARCHIVE_FILENAME, GacosAdapter, build_form
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.errors import CredentialsMissingError
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "chamoli-rishiganga"
BBOX = (79.68, 30.33, 79.80, 30.42)
T0 = datetime(2021, 2, 5, tzinfo=UTC)
T1 = datetime(2021, 2, 9, 23, 59, 59, tzinfo=UTC)


def settings(**kw: Any) -> SeracSettings:
    return SeracSettings(_env_file=None, **kw)  # type: ignore[call-arg]


# -- ERA5 ---------------------------------------------------------------------------------------


class FakeCds:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Path]] = []

    def retrieve(self, dataset: str, request: dict[str, Any], target: Path) -> None:
        self.calls.append((dataset, request, target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"CDF\x01fake")


def test_era5_area_and_request_arithmetic() -> None:
    area = era5_area(BBOX)
    assert area == (30.5, 79.5, 30.25, 80.0)  # N, W, S, E grown outward to 0.25 deg
    assert era5_cells(area) == (2, 3)
    req = build_cds_request(BBOX, T0, T1, ("2m_temperature",))
    assert req["year"] == ["2021"] and req["month"] == ["02"]
    assert req["day"] == ["05", "06", "07", "08", "09"]
    assert len(req["time"]) == 24 and req["data_format"] == "netcdf"


def test_era5_plan_is_arithmetic_and_needs_key() -> None:
    adapter = Era5Adapter(cds=FakeCds(), settings=settings(), git_sha=None)
    plan = adapter.plan(IngestRequest(aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1))
    assert len(plan.products) == 1
    p = plan.products[0]
    assert p.product_id == f"{ERA5_DATASET}_{AOI}_20210205_20210209"
    assert plan.estimated_bytes == 2 * 3 * 24 * 5 * 1 * 4
    assert "2 x 3 grid points" in plan.estimate_basis
    assert [c.name for c in plan.requires_credentials] == ["CDS API key"]
    with pytest.raises(ValueError, match="time_start"):
        adapter.plan(IngestRequest(aoi_id=AOI, bbox_4326=BBOX))


def test_era5_fetch_paths(tmp_path: Path) -> None:
    plan_request = IngestRequest(aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1)
    no_key = Era5Adapter(cds=FakeCds(), settings=settings(), git_sha=None)
    plan = no_key.plan(plan_request)
    ledger = JsonlManifestLedger(tmp_path / "a.jsonl")
    with pytest.raises(CredentialsMissingError, match="CDS"):
        no_key.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert [e.status for e in ledger.entries()] == [ManifestStatus.not_fetched]

    cds = FakeCds()
    keyed = Era5Adapter(
        cds=cds, settings=settings(cdsapi_key=SecretStr("k")), repo_root=tmp_path, git_sha=None
    )
    plan = keyed.plan(plan_request)
    ledger = JsonlManifestLedger(tmp_path / "b.jsonl")
    entries = keyed.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert len(entries) == 1 and entries[0].status is ManifestStatus.fetched
    assert cds.calls[0][0] == ERA5_DATASET and cds.calls[0][1]["area"] == [30.5, 79.5, 30.25, 80.0]
    assert entries[0].path and entries[0].path.endswith("era5.nc")
    assert entries[0].params["cds_request"]["variable"] == ["2m_temperature"]


# -- GACOS --------------------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, *, head_ok: bool = True) -> None:
        self.head_ok = head_ok
        self.streamed: list[str] = []

    def stream_to(self, url: str, dest: Path) -> tuple[str, int]:
        import hashlib

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ztd-archive")
        self.streamed.append(url)
        return hashlib.sha256(b"ztd-archive").hexdigest(), len(b"ztd-archive")

    def head_content_length(self, url: str) -> int | None:
        if not self.head_ok:
            raise OSError("404")
        return 11

    def get_json(self, url: str) -> Any:
        raise NotImplementedError


class FakeForm:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, data: dict[str, str]) -> str:
        self.posts.append((url, data))
        return "Your request has been received"


def gacos_request() -> IngestRequest:
    return IngestRequest(
        aoi_id=AOI, bbox_4326=BBOX, params={"dates": ["20210130", "20210211"], "time_utc": "00:43"}
    )


def test_gacos_form_values() -> None:
    form = build_form(gacos_request(), "ops@example.invalid")
    assert form.as_form() == {
        "N": "30.4200",
        "S": "30.3300",
        "W": "79.6800",
        "E": "79.8000",
        "date": "20210130,20210211",
        "time": "00:43",
        "email": "ops@example.invalid",
    }
    with pytest.raises(ValueError, match="dates"):
        build_form(IngestRequest(aoi_id=AOI, bbox_4326=BBOX), "x")
    with pytest.raises(ValueError):
        build_form(IngestRequest(aoi_id=AOI, bbox_4326=BBOX, params={"dates": ["2021-01-30"]}), "x")


def test_gacos_request_without_email_records_not_fetched(tmp_path: Path) -> None:
    adapter = GacosAdapter(http=FakeHttp(), settings=settings(), git_sha=None)
    plan = adapter.plan(gacos_request())
    assert plan.estimated_bytes is None and [c.name for c in plan.requires_credentials] == [
        "GACOS delivery e-mail"
    ]
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(CredentialsMissingError, match="GACOS_EMAIL"):
        adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    assert [e.status for e in ledger.entries()] == [ManifestStatus.not_fetched]


def test_gacos_request_poll_receive(tmp_path: Path) -> None:
    http = FakeHttp()
    form = FakeForm()
    adapter = GacosAdapter(
        http=http,
        form_client=form,
        form_url="https://gacos.invalid/form",
        settings=settings(gacos_email="ops@example.invalid"),
        repo_root=tmp_path,
        git_sha=None,
    )
    plan = adapter.plan(gacos_request())
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    requested = adapter.request(plan, ledger=ledger, request_id="req-001")
    assert requested.status is ManifestStatus.requested and requested.source is DataSource.gacos
    assert requested.params["request_id"] == "req-001"
    assert requested.params["form"]["email"] == "<redacted>"  # never the address itself
    assert form.posts[0][1]["email"] == "ops@example.invalid"
    assert requested.params["acknowledgement"] == "Your request has been received"

    assert adapter.poll(ledger, "req-001")["state"] == "requested"
    assert adapter.poll(ledger, "nope")["state"] == "unknown"
    late = adapter.poll(ledger, "req-001", receive_url="https://gacos.invalid/dl/x.tar.gz")
    assert late["link_available"] is True and late["link_content_length"] == 11
    assert (
        GacosAdapter(http=FakeHttp(head_ok=False), settings=settings(), git_sha=None).poll(
            ledger, "req-001", receive_url="https://gacos.invalid/dl/x.tar.gz"
        )["link_available"]
        is False
    )

    fetched = adapter.receive(
        ledger, request_id="req-001", url="https://gacos.invalid/dl/x.tar.gz", dest_root=tmp_path
    )
    assert fetched.status is ManifestStatus.fetched
    assert (
        fetched.path
        and fetched.path.endswith(ARCHIVE_FILENAME)
        and (tmp_path / fetched.path).exists()
    )
    assert (
        fetched.params["request_id"] == "req-001"
        and fetched.params["requested_entry_id"] == requested.entry_id
    )
    assert http.streamed == ["https://gacos.invalid/dl/x.tar.gz"]
    assert adapter.poll(ledger, "req-001")["state"] == "fetched"
    with pytest.raises(ValueError, match="no GACOS request"):
        adapter.receive(ledger, request_id="zzz", url="https://x.invalid", dest_root=tmp_path)


def test_gacos_manual_submission_is_the_default(tmp_path: Path) -> None:
    adapter = GacosAdapter(
        http=FakeHttp(), settings=settings(gacos_email="ops@example.invalid"), git_sha=None
    )
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    entry = adapter.request(adapter.plan(gacos_request()), ledger=ledger)
    assert entry.params["form_url"] is None and entry.params["acknowledgement"] is None
    assert entry.notes and "by hand" in entry.notes and entry.params["request_id"] in entry.notes
