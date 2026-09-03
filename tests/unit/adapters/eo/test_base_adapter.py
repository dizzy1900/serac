"""`BaseIngestAdapter` mechanics through a fake adapter: no network, no real products."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile, product_dir
from serac.adapters.eo._http import sha256_and_size
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.errors import CredentialsMissingError, FetchDeclinedError, IngestRefusedError
from serac.ports.ingest import CredentialSpec, DryRunPlan, IngestRequest, ProductRecord
from serac.settings import SeracSettings

BBOX = (79.68, 30.33, 79.80, 30.42)
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class FakeAdapter(BaseIngestAdapter):
    source: ClassVar[DataSource] = DataSource.usgs_comcat
    adapter_name: ClassVar[str] = "fake"
    adapter_version: ClassVar[str] = "9.9.9"
    licence: ClassVar[str] = "US-PD"
    licence_source_url: ClassVar[str | None] = "https://example.invalid/licence"

    def __init__(self, payloads: dict[str, bytes], *, fail_on: str | None = None, **kw: object):
        super().__init__(**kw)  # type: ignore[arg-type]
        self.payloads = payloads
        self.fail_on = fail_on
        self.fetched: list[str] = []

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        return [
            ProductRecord(
                source=self.source,
                product_id=pid,
                url=f"https://example.invalid/{pid}",
                estimated_bytes=len(data),
                licence=self.licence,
                licence_source_url=self.licence_source_url,
                time_start=NOW,
                time_end=NOW,
            )
            for pid, data in self.payloads.items()
        ]

    def plan(self, request: IngestRequest) -> DryRunPlan:
        products = self.search(request)
        est = request.params.get("estimate_override", sum(len(d) for d in self.payloads.values()))
        return self.build_plan(
            request,
            products,
            estimated_bytes=est,
            estimate_basis="len(payload)",
            refusals=request.params.get("refusals", ()),
        )

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        if product.product_id == self.fail_on:
            raise OSError("disk on fire")
        out = dest / f"{product.product_id}.bin"
        out.write_bytes(self.payloads[product.product_id])
        sha, size = sha256_and_size(out)
        self.fetched.append(product.product_id)
        return [FetchedFile(path=out, sha256=sha, size_bytes=size, params={"kind": "blob"})]


def _settings(**kw: object) -> SeracSettings:
    return SeracSettings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.fixture
def ledger(tmp_path: Path) -> JsonlManifestLedger:
    return JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")


def _request(**params: object) -> IngestRequest:
    return IngestRequest(aoi_id="chamoli-rishiganga", bbox_4326=BBOX, event_id="ev", params=params)


def test_plan_fetch_ledger_roundtrip(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter(
        {"a": b"alpha", "b": b"bravo!"},
        settings=_settings(),
        repo_root=tmp_path,
        git_sha="f" * 40,
        clock=lambda: NOW,
    )
    plan = adapter.plan(_request(tag=1))
    assert plan.estimated_bytes == 11 and plan.fetchable and plan.warnings == []
    assert plan.requires_credentials == []
    confirmations: list[str] = []

    def confirm(q: str) -> bool:
        confirmations.append(q)
        return True

    entries = adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=confirm)
    assert confirmations == []  # under the gate: never asked
    assert [e.product_id for e in entries] == ["a", "b"]
    expected_dir = product_dir(tmp_path / "data", DataSource.usgs_comcat, "chamoli-rishiganga", "a")
    assert expected_dir == tmp_path / "data" / "raw" / "usgs_comcat" / "chamoli-rishiganga" / "a"
    a = entries[0]
    assert a.status is ManifestStatus.fetched and a.provenance is Provenance.real
    assert a.path == "data/raw/usgs_comcat/chamoli-rishiganga/a/a.bin"
    assert (tmp_path / a.path).read_bytes() == b"alpha"
    assert a.sha256 == sha256_and_size(tmp_path / a.path)[0] and a.size_bytes == 5
    assert a.adapter == "fake" and a.adapter_version == "9.9.9" and a.serac_git_sha == "f" * 40
    assert a.retrieved_at == NOW and a.recorded_at == NOW
    assert a.params == {"request": {"tag": 1}, "kind": "blob"}
    assert a.licence == "US-PD" and a.licence_source_url == "https://example.invalid/licence"
    assert a.aoi_id == "chamoli-rishiganga" and a.event_id == "ev" and a.bbox_4326 == BBOX
    assert a.url == "https://example.invalid/a" and a.estimated_bytes == 5
    # the ledger on disk holds exactly these two lines
    assert [e.entry_id for e in ledger.entries()] == [e.entry_id for e in entries]


def test_plan_writes_nothing(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter({"a": b"x"}, settings=_settings(), repo_root=tmp_path)
    adapter.plan(_request())
    assert not ledger.path.exists()
    assert not (tmp_path / "data" / "raw").exists()


def test_size_gate_asks_and_records_not_fetched_when_declined(
    tmp_path: Path, ledger: JsonlManifestLedger
) -> None:
    adapter = FakeAdapter({"a": b"x"}, settings=_settings(), repo_root=tmp_path)
    adapter.size_gate_bytes = 10  # type: ignore[misc]
    plan = adapter.plan(_request(estimate_override=11))
    assert any("exceeds" in w for w in plan.warnings)
    asked: list[str] = []

    def decline(q: str) -> bool:
        asked.append(q)
        return False

    with pytest.raises(FetchDeclinedError):
        adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=decline)
    assert len(asked) == 1 and "11 B" in asked[0] and "gate" in asked[0]
    recorded = list(ledger.entries())
    assert [e.status for e in recorded] == [ManifestStatus.not_fetched]
    assert recorded[0].notes is not None and "declined" in recorded[0].notes
    assert adapter.fetched == []


def test_size_gate_proceeds_when_confirmed(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter({"a": b"x"}, settings=_settings(), repo_root=tmp_path)
    adapter.size_gate_bytes = 0  # type: ignore[misc]
    plan = adapter.plan(_request())
    entries = adapter.fetch(
        plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: True
    )
    assert [e.status for e in entries] == [ManifestStatus.fetched]


def test_unknown_size_also_asks(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter({"a": b"x"}, settings=_settings(), repo_root=tmp_path)
    plan = adapter.plan(_request(estimate_override=None))
    assert plan.estimated_bytes is None
    assert any("cannot be estimated" in w for w in plan.warnings)
    asked: list[str] = []
    with pytest.raises(FetchDeclinedError):
        adapter.fetch(
            plan,
            dest_root=tmp_path / "data",
            ledger=ledger,
            confirm=lambda q: asked.append(q) or False,
        )
    assert asked and "unknown size" in asked[0]


class CredentialedAdapter(FakeAdapter):
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (
        CredentialSpec(
            name="Earthdata Login",
            env_vars=("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"),
            purpose="download",
        ),
    )


def test_missing_credentials_record_not_fetched_and_raise(
    tmp_path: Path, ledger: JsonlManifestLedger
) -> None:
    adapter = CredentialedAdapter({"a": b"x", "b": b"y"}, settings=_settings(), repo_root=tmp_path)
    plan = adapter.plan(_request())
    assert [c.name for c in plan.requires_credentials] == ["Earthdata Login"]
    with pytest.raises(CredentialsMissingError, match="EARTHDATA_USERNAME"):
        adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: True)
    recorded = list(ledger.entries())
    assert [e.status for e in recorded] == [ManifestStatus.not_fetched] * 2
    assert all(e.notes and "credentials missing" in e.notes for e in recorded)
    assert all(e.path is None and e.sha256 is None for e in recorded)
    assert adapter.fetched == []


def test_present_credentials_allow_fetch(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    settings = _settings(earthdata_username="u", earthdata_password="p")
    adapter = CredentialedAdapter({"a": b"x"}, settings=settings, repo_root=tmp_path)
    plan = adapter.plan(_request())
    assert plan.requires_credentials == []
    entries = adapter.fetch(
        plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: True
    )
    assert [e.status for e in entries] == [ManifestStatus.fetched]


def test_refusals_block_fetch(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter({"a": b"x"}, settings=_settings(), repo_root=tmp_path)
    plan = adapter.plan(_request(refusals=["BETA and PROVISIONAL mixed"]))
    assert not plan.fetchable
    with pytest.raises(IngestRefusedError, match="mixed"):
        adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: True)
    assert not ledger.path.exists()


def test_failure_is_recorded_then_reraised(tmp_path: Path, ledger: JsonlManifestLedger) -> None:
    adapter = FakeAdapter(
        {"a": b"x", "b": b"y"}, fail_on="b", settings=_settings(), repo_root=tmp_path
    )
    plan = adapter.plan(_request())
    with pytest.raises(OSError, match="disk on fire"):
        adapter.fetch(plan, dest_root=tmp_path / "data", ledger=ledger, confirm=lambda _q: True)
    statuses = [(e.product_id, e.status) for e in ledger.entries()]
    assert statuses == [("a", ManifestStatus.fetched), ("b", ManifestStatus.failed)]
    failed = list(ledger.entries())[1]
    assert failed.notes == "OSError: disk on fire"


def test_git_sha_is_none_outside_a_checkout(tmp_path: Path) -> None:
    adapter = FakeAdapter({}, settings=_settings(), repo_root=tmp_path)
    assert adapter._git_sha is None
