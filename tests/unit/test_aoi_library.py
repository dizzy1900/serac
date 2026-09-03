"""Every committed AOI validates, carries the fixed transect ids, and cites real sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.cli_aoi import app
from serac.domain.common import BEST_QUALIFYING_KINDS, SourceKind
from serac.domain.manifest import DataSource, ManifestStatus
from serac.pipelines.aoi_build import FixtureOverpassClient, build_aoi, iter_aoi_dirs, read_aoi_dir
from serac.pipelines.aoi_specs import AOI_SPECS, FIXED_TRANSECT_IDS
from serac.validation.aoi import run_suite
from serac.validation.result import Severity

EXPECTED_AOIS = frozenset(AOI_SPECS)


def _aoi_dirs(repo_root: Path) -> list[Path]:
    return list(iter_aoi_dirs(repo_root))


def test_all_three_aois_are_committed(repo_root: Path) -> None:
    assert {p.name for p in _aoi_dirs(repo_root)} == EXPECTED_AOIS


def test_suite_passes_on_committed_library(repo_root: Path) -> None:
    result = run_suite(repo_root)
    assert result.passed, [c.name + ": " + c.details for c in result.checks if c.failed]
    warnings = [c for c in result.checks if c.severity is Severity.warning and not c.ok]
    # hand-digitised geometry is expected (source zones, DoED rectangles) and must be visible
    assert warnings, "the hand-digitised warning should list the source zones"


@pytest.mark.parametrize("aoi_id", sorted(EXPECTED_AOIS))
def test_fixed_transect_ids_exist(aoi_id: str, repo_root: Path) -> None:
    files = read_aoi_dir(repo_root / "data" / "aoi" / aoi_id)
    ids = [t.id for t in files.transects]
    assert list(FIXED_TRANSECT_IDS[aoi_id]) == ids
    chainages = [t.chainage_km for t in sorted(files.transects, key=lambda t: t.chainage_km)]
    assert chainages == sorted(chainages) and chainages[0] >= 0.0


@pytest.mark.parametrize("aoi_id", sorted(EXPECTED_AOIS))
def test_every_asset_has_a_resolvable_source(aoi_id: str, repo_root: Path) -> None:
    files = read_aoi_dir(repo_root / "data" / "aoi" / aoi_id)
    sources = {s.id: s for s in files.aoi.sources}
    assert files.assets, "an AOI without exposed assets is not what the brief asked for"
    for asset in files.assets:
        assert asset.source_refs and all(r in sources for r in asset.source_refs), asset.id
        if asset.capacity_mw is not None and asset.capacity_mw.best is not None:
            kinds = {sources[r].kind for r in asset.capacity_mw.source_refs}
            assert kinds & BEST_QUALIFYING_KINDS, f"{asset.id}: best without a qualifying source"
        if asset.population is not None and asset.population.best is not None:
            kinds = {sources[r].kind for r in asset.population.source_refs}
            assert kinds & BEST_QUALIFYING_KINDS, f"{asset.id}: best without a qualifying source"


@pytest.mark.parametrize("aoi_id", sorted(EXPECTED_AOIS))
def test_sources_are_hashed_and_dated(aoi_id: str, repo_root: Path) -> None:
    files = read_aoi_dir(repo_root / "data" / "aoi" / aoi_id)
    for s in files.aoi.sources:
        assert s.accessed_utc.year == 2026 and s.sha256 != "0" * 64
        assert "wikipedia" not in s.url.lower()
        if s.kind is SourceKind.press_report:
            assert s.year is not None and s.year >= 2025, f"{s.id}: press only for 2025-2026"
        if s.stored_copy:
            digest = hashlib.sha256((repo_root / s.stored_copy).read_bytes()).hexdigest()
            assert digest == s.sha256, f"{s.id}: stored copy drifted"


def test_requested_lhende_assets_present(repo_root: Path) -> None:
    files = read_aoi_dir(repo_root / "data" / "aoi" / "lhende-khola-trishuli")
    by_id = {a.id: a for a in files.assets}
    for asset_id, mw in (
        ("rasuwagadhi-hep", 111.0),
        ("upper-trishuli-3a", 60.0),
        ("chilime-hep", 22.0),
    ):
        assert by_id[asset_id].capacity_mw is not None
        assert by_id[asset_id].capacity_mw.best == mw  # type: ignore[union-attr]
    assert {
        "timure",
        "syabrubesi",
        "betrawati",
        "miteri-bridge",
        "rasuwagadhi-kerung-border-post",
    } <= set(by_id)
    for a in by_id.values():
        if a.geometry_quality.value == "hand_digitised_approximate":
            assert "licence rectangle" in (a.notes or ""), a.id


def test_chamoli_and_blatten_assets(repo_root: Path) -> None:
    chamoli = {
        a.id: a for a in read_aoi_dir(repo_root / "data" / "aoi" / "chamoli-rishiganga").assets
    }
    assert chamoli["rishiganga-hep"].capacity_mw.best == 13.2  # type: ignore[union-attr]
    assert chamoli["tapovan-vishnugad-hep"].capacity_mw.best == 520.0  # type: ignore[union-attr]
    blatten = {
        a.id: a for a in read_aoi_dir(repo_root / "data" / "aoi" / "blatten-lotschental").assets
    }
    assert blatten["blatten"].status.value == "destroyed"
    assert blatten["blatten"].population is not None


def test_overpass_fixtures_have_ledger_rows(repo_root: Path) -> None:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    rows = {e.path: e for e in ledger.entries() if e.source is DataSource.osm_overpass}
    md = (repo_root / "data" / "fixtures" / "FIXTURES.md").read_text(encoding="utf-8")
    for spec in AOI_SPECS.values():
        row = rows.get(spec.fixture_path)
        assert row is not None, f"{spec.fixture_path} has no osm_overpass ledger row"
        assert row.status is ManifestStatus.fetched and row.aoi_id == spec.id
        assert row.licence == "ODbL-1.0" and row.licence_source_url
        assert (
            row.sha256 == hashlib.sha256((repo_root / spec.fixture_path).read_bytes()).hexdigest()
        )
        assert row.retrieved_at == spec.fixture_retrieved_utc
        assert f"`{spec.fixture_path}`" in md and row.sha256 in md
        assert " ".join(spec.overpass_query.split()) == row.params["data"]


def test_source_documents_have_ledger_rows(repo_root: Path) -> None:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    by_sha = {e.sha256 for e in ledger.entries() if e.source is DataSource.source_document}
    for spec in AOI_SPECS.values():
        for s in spec.sources:
            if s.kind is SourceKind.usgs_comcat:
                continue  # recorded by the seismic fixture fetch
            assert s.sha256 in by_sha, f"{spec.id}/{s.id}: no source_document ledger row"


@pytest.mark.parametrize("aoi_id", sorted(EXPECTED_AOIS))
def test_offline_rebuild_reproduces_committed_files(aoi_id: str, repo_root: Path) -> None:
    spec = AOI_SPECS[aoi_id]
    built = build_aoi(spec, FixtureOverpassClient(repo_root / spec.fixture_path))
    committed = read_aoi_dir(repo_root / "data" / "aoi" / aoi_id)
    assert built.aoi == committed.aoi
    assert built.transects == committed.transects
    assert built.assets == committed.assets


def test_cli_validate_and_describe(repo_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["validate", "--repo", str(repo_root), "--report-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "aoi.json").read_text())
    assert report["suite"] == "aoi"
    result = runner.invoke(
        app, ["describe", "--aoi", "lhende-khola-trishuli", "--repo", str(repo_root)]
    )
    assert result.exit_code == 0, result.output
    assert "galchhi" in result.output and "rasuwagadhi-hep" in result.output
    result = runner.invoke(app, ["describe", "--aoi", "nope", "--repo", str(repo_root)])
    assert result.exit_code == 2


def test_cli_build_offline_into_tmp_repo(repo_root: Path, tmp_path: Path) -> None:
    spec = AOI_SPECS["blatten-lotschental"]
    fixture = tmp_path / spec.fixture_path
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes((repo_root / spec.fixture_path).read_bytes())
    result = CliRunner().invoke(
        app, ["build", "--aoi", spec.id, "--offline", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "aoi" / spec.id / "aoi.json").exists()
    assert "transect gampel" in result.output
