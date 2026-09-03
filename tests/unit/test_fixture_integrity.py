"""Every committed fixture is exactly what its manifest and the ledger say it is."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.domain.replay import FixtureManifest

FIXTURE_ROOTS = ("data/fixtures/", "contracts/vendor/")


def _seismic_manifests(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "data" / "fixtures" / "seismic").glob("*/manifest.json"))


@pytest.fixture(scope="module")
def ledger(repo_root: Path) -> JsonlManifestLedger:
    path = repo_root / "data" / "manifest.jsonl"
    assert path.exists(), "data/manifest.jsonl is missing"
    return JsonlManifestLedger(path)


@pytest.fixture(scope="module")
def ledger_index(ledger: JsonlManifestLedger) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for entry in ledger.entries():
        if entry.path and entry.sha256:
            index.setdefault(entry.path, set()).add(entry.sha256)
    return index


def test_seismic_fixture_directories_exist(repo_root: Path) -> None:
    names = {p.parent.name for p in _seismic_manifests(repo_root)}
    assert {"chamoli-2021", "langtang-2026"} <= names


@pytest.mark.parametrize(
    "manifest_path",
    _seismic_manifests(Path(__file__).resolve().parents[2]),
    ids=lambda p: p.parent.name,
)
def test_seismic_fixture_manifest(
    manifest_path: Path, repo_root: Path, ledger_index: dict[str, set[str]]
) -> None:
    manifest = FixtureManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert manifest.event_id == manifest_path.parent.name
    assert manifest.status in ("fetched", "partial")
    assert manifest.request.base_url.startswith("https://")
    assert manifest.request.base_url != manifest.request.client, "alias recorded as base_url"
    assert manifest.licence_source_url, "licence_source_url must always be recorded"
    kinds = {f.kind for f in manifest.files}
    assert {"miniseed", "stationxml"} <= kinds
    for file in manifest.files:
        path = manifest_path.parent / file.path
        assert path.exists(), f"{file.path} listed but missing"
        assert sha256_of_file(path) == file.sha256, f"{file.path} sha256 drifted"
        assert path.stat().st_size == file.size_bytes
        rel = path.relative_to(repo_root).as_posix()
        assert file.sha256 in ledger_index.get(rel, set()), f"{rel} has no ledger row"
        if file.kind == "miniseed":
            assert file.sncl and file.start_utc and file.end_utc and file.npts
            assert manifest.window.start_utc <= file.start_utc
            assert file.end_utc <= manifest.window.end_utc
            assert file.sncl in {
                row[0] + "." + row[1] + "." + row[2].replace("--", "") + "." + row[3]
                for row in manifest.request.bulk
            }
    listed = {f.path for f in manifest.files}
    on_disk = {p.name for p in manifest_path.parent.iterdir() if p.name != "manifest.json"}
    assert on_disk == listed, "fixture directory has files not covered by manifest.json"


def test_cap_vendor_manifest(repo_root: Path, ledger_index: dict[str, set[str]]) -> None:
    cap_dir = repo_root / "contracts" / "vendor" / "cap"
    manifest = json.loads((cap_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    files = {f["file"]: f for f in manifest["files"]}
    assert "CAP-v1.2.xsd" in files
    assert files["CAP-v1.2.xsd"]["status"] == "fetched"
    for name, meta in files.items():
        if meta["status"] != "fetched":
            continue
        path = cap_dir / name
        assert path.exists()
        assert sha256_of_file(path) == meta["sha256"]
        assert path.stat().st_size == meta["size_bytes"]
        assert meta["licence_source_url"]
        rel = path.relative_to(repo_root).as_posix()
        assert meta["sha256"] in ledger_index.get(rel, set()), f"{rel} has no ledger row"
    xsd = (cap_dir / "CAP-v1.2.xsd").read_text(encoding="utf-8")
    assert 'targetNamespace = "urn:oasis:names:tc:emergency:cap:1.2"' in xsd


def test_comcat_fixtures(repo_root: Path, ledger_index: dict[str, set[str]]) -> None:
    comcat = repo_root / "data" / "fixtures" / "usgs_comcat"
    files = sorted(comcat.glob("*.geojson"))
    names = {p.name for p in files}
    assert {"us7000tbwb.geojson", "us7000tc90.geojson", "us20002926.geojson"} <= names
    assert any(n.startswith("landslide_2000-01-01_") for n in names)
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(repo_root).as_posix()
        assert sha256_of_file(path) in ledger_index.get(rel, set()), f"{rel} has no ledger row"
        if path.name.startswith("landslide_"):
            assert doc["type"] == "FeatureCollection"
            # ComCat is not case-consistent: two Alaska entries are typed "Landslide".
            assert all(f["properties"]["type"].lower() == "landslide" for f in doc["features"])
        else:
            assert doc["type"] == "Feature"
            assert doc["id"] == path.stem


def test_ledger_rows_for_fixtures_are_real_and_hashed(
    ledger: JsonlManifestLedger, repo_root: Path
) -> None:
    rows = [e for e in ledger.entries() if e.path and e.path.startswith(FIXTURE_ROOTS)]
    assert rows, "no fixture rows in the ledger"
    for entry in rows:
        assert entry.path is not None
        assert entry.provenance == Provenance.real
        assert entry.status == ManifestStatus.fetched
        assert (
            entry.source
            in {
                DataSource.fdsn_waveforms,
                DataSource.usgs_comcat,
                DataSource.vendored_schema,
            }
            or entry.adapter != "fixture-fetch"
        )
        assert entry.url and entry.retrieved_at and entry.sha256 and entry.size_bytes is not None
        assert entry.licence_source_url, f"{entry.path}: licence_source_url missing"
        path = repo_root / entry.path
        assert path.exists(), f"{entry.path}: ledger row without file"
        assert sha256_of_file(path) == entry.sha256, f"{entry.path}: sha256 drifted"
        assert path.stat().st_size == entry.size_bytes


def test_no_synthetic_rows_under_data(ledger: JsonlManifestLedger) -> None:
    for entry in ledger.entries():
        if entry.path and entry.path.startswith("data/"):
            assert entry.provenance != Provenance.synthetic


def test_fixtures_md_covers_every_fixture_row(ledger: JsonlManifestLedger, repo_root: Path) -> None:
    text = (repo_root / "data" / "fixtures" / "FIXTURES.md").read_text(encoding="utf-8")
    for entry in ledger.entries():
        if entry.path and entry.path.startswith(FIXTURE_ROOTS) and entry.adapter == "fixture-fetch":
            assert f"`{entry.path}`" in text, f"{entry.path} missing from FIXTURES.md"
            assert entry.sha256 is not None
            assert entry.sha256 in text
