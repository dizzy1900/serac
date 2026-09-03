"""validate-ingest: passes on the committed tree; corrupted ledgers fail the right rule."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.validation.ingest import run_suite
from serac.validation.result import SuiteResult


def failed(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if c.failed}


def names(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks}


def copy_tree(repo_root: Path, tmp_path: Path) -> Path:
    fake = tmp_path / "repo"
    shutil.copytree(repo_root / "data" / "fixtures", fake / "data" / "fixtures")
    # Every committed, non-DVC path the ledger references has to exist in the fake tree, or
    # `ingest.committed_files_present` fails for a reason the test is not about. `data/regions`
    # holds the discriminator's stratification artefact.
    shutil.copytree(repo_root / "data" / "regions", fake / "data" / "regions")
    shutil.copy(repo_root / "data" / "manifest.jsonl", fake / "data" / "manifest.jsonl")
    shutil.copytree(repo_root / "tests" / "fixtures", fake / "tests" / "fixtures")
    shutil.copytree(repo_root / "contracts", fake / "contracts")  # vendored CAP schemas
    return fake


def nisar_row(aoi: str, level: str, path: str, sha: str) -> ManifestEntry:
    return ManifestEntry(
        source=DataSource.nisar_asf,
        product_id=f"NISAR_{level}_{path[-6:]}",
        product_level=level,
        aoi_id=aoi,
        path=path,
        url="https://example.invalid/nisar",
        sha256=sha,
        size_bytes=2,
        retrieved_at=datetime(2026, 9, 3, tzinfo=UTC),
        licence="NASA open",
        provenance=Provenance.real,
        status=ManifestStatus.fetched,
        adapter="test",
        adapter_version="0",
    )


def test_passes_on_committed_tree(repo_root: Path) -> None:
    result = run_suite(repo_root)
    assert result.passed, [c for c in result.checks if c.failed]
    expected = {
        "ingest.ledger_parses",
        "ingest.fetched_rows_rehash",
        "ingest.committed_files_present",
        "ingest.not_fetched_rows_have_no_file",
        "ingest.fetched_rows_carry_provenance",
        "ingest.no_real_row_under_synthetic_dir",
        "ingest.no_synthetic_under_data",
        "ingest.synthetic_rows_labelled",
        "ingest.synthetic_files_recorded",
        "ingest.nisar_levels_not_mixed",
        "ingest.fixture_files_recorded",
    }
    assert expected <= names(result)
    rehash = next(c for c in result.checks if c.name == "ingest.fetched_rows_rehash")
    assert "re-hashed" in rehash.details


def test_missing_ledger(tmp_path: Path) -> None:
    assert failed(run_suite(tmp_path)) == {"ingest.ledger_exists"}


def test_unparseable_ledger(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    with (fake / "data" / "manifest.jsonl").open("a") as fh:
        fh.write('{"not": "a manifest entry"}\n')
    assert failed(run_suite(fake)) == {"ingest.ledger_parses"}


def test_tampered_fixture_fails_rehash(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    target = fake / "data" / "fixtures" / "asf" / "chamoli_s1_2021-01-01_2021-02-28.geojson"
    target.write_bytes(target.read_bytes() + b"\n")
    assert failed(run_suite(fake)) == {"ingest.fetched_rows_rehash"}


def test_unrecorded_fixture_file(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    (fake / "data" / "fixtures" / "stray.bin").write_bytes(b"stray")
    assert failed(run_suite(fake)) == {"ingest.fixture_files_recorded"}
    (fake / "data" / "fixtures" / "stray.bin").unlink()
    (fake / "tests" / "fixtures" / "synthetic" / "stray.tif").write_bytes(b"stray")
    assert failed(run_suite(fake)) == {"ingest.synthetic_files_recorded"}


def test_synthetic_under_data_and_real_under_synthetic(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    ledger_path = fake / "data" / "manifest.jsonl"
    rows = [
        json.loads(line) for line in ledger_path.read_text("utf-8").splitlines() if line.strip()
    ]
    synthetic = next(r for r in rows if r["provenance"] == "synthetic")
    real = next(
        r for r in rows if r["status"] == "fetched" and r["path"].startswith("data/fixtures/")
    )
    # bypass the pydantic guard on purpose: the suite must catch what a hand-edited ledger says
    tampered_real = dict(real, path="tests/fixtures/synthetic/hyp3/x.tif", entry_id="a" * 32)
    tampered_synth = dict(synthetic, path="data/fixtures/fake.tif", entry_id="b" * 32)
    with ledger_path.open("a") as fh:
        fh.write(json.dumps(tampered_real) + "\n")
    result = run_suite(fake)
    assert "ingest.no_real_row_under_synthetic_dir" in failed(result)
    with ledger_path.open("a") as fh:
        fh.write(json.dumps(tampered_synth) + "\n")
    result = run_suite(fake)
    # the manifest contract itself refuses synthetic paths under data/: the ledger stops parsing
    assert failed(result) == {"ingest.ledger_parses"}


def test_nisar_level_mixing_is_caught(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    raw = fake / "data" / "raw" / "nisar_asf" / "lhende-khola-trishuli"
    raw.mkdir(parents=True)
    import hashlib

    sha = hashlib.sha256(b"h5").hexdigest()
    ledger = JsonlManifestLedger(fake / "data" / "manifest.jsonl")
    for level, name in (("BETA", "a.h5"), ("PROVISIONAL", "b.h5")):
        (raw / name).write_bytes(b"h5")
        ledger.append(
            nisar_row(
                "lhende-khola-trishuli",
                level,
                f"data/raw/nisar_asf/lhende-khola-trishuli/{name}",
                sha,
            )
        )
    result = run_suite(fake)
    assert failed(result) == {"ingest.nisar_levels_not_mixed"}
    detail = next(c for c in result.checks if c.name == "ingest.nisar_levels_not_mixed").details
    assert "lhende-khola-trishuli" in detail and "beta" in detail and "provisional" in detail


def test_not_fetched_row_pointing_at_a_file(repo_root: Path, tmp_path: Path) -> None:
    fake = copy_tree(repo_root, tmp_path)
    ledger = JsonlManifestLedger(fake / "data" / "manifest.jsonl")
    ledger.append(
        ManifestEntry(
            source=DataSource.era5_cds,
            product_id="era5-x",
            aoi_id="chamoli-rishiganga",
            path="data/fixtures/FIXTURES.md",
            licence="x",
            provenance=Provenance.real,
            status=ManifestStatus.not_fetched,
            adapter="test",
            adapter_version="0",
        )
    )
    assert failed(run_suite(fake)) == {"ingest.not_fetched_rows_have_no_file"}
