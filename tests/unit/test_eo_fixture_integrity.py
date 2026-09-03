"""Every committed fixture is real, recorded, and byte-for-byte what the ledger says it is.

Rules enforced here (see `serac.domain.manifest`):

* every `data/manifest.jsonl` line parses as a `ManifestEntry`;
* every entry with a path under `data/fixtures/` points at an existing file whose sha256 and
  size match, and carries a URL, licence source, retrieval time and bbox;
* every file under `data/fixtures/` (documentation aside) has a ledger entry;
* every row of `data/fixtures/FIXTURES.md` agrees with the ledger;
* nothing under `data/` is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import ManifestEntry, ManifestStatus, Provenance

DOC_FILES = {"FIXTURES.md", ".gitkeep", "README.md"}
ROW = re.compile(
    r"^\| `(?P<path>data/fixtures/[^`]+)` \| (?P<url>\S+) \| \S+ \| "
    r"`(?P<sha>[0-9a-f]{64})` \| (?P<size>\d+) \|"
)


@pytest.fixture(scope="module")
def entries(repo_root: Path) -> list[ManifestEntry]:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    found = list(ledger.entries())
    if not found:
        pytest.skip("data/manifest.jsonl is empty")
    return found


EO_PREFIXES = (
    "data/fixtures/dem_glo30/",
    "data/fixtures/sentinel2/",
    "data/fixtures/asf/",
    "data/fixtures/cdse/",
)


def is_eo_fixture(path: str) -> bool:
    """EO-lane fixtures only; seismic/ComCat fixtures are checked by test_fixture_integrity."""
    return path.startswith(EO_PREFIXES)


@pytest.fixture(scope="module")
def latest_by_path(entries: list[ManifestEntry]) -> dict[str, ManifestEntry]:
    latest: dict[str, ManifestEntry] = {}
    for e in entries:
        if e.path is not None and is_eo_fixture(e.path):
            prev = latest.get(e.path)
            if prev is None or e.recorded_at >= prev.recorded_at:
                latest[e.path] = e
    return latest


def test_ledger_has_eo_fixture_entries(latest_by_path: dict[str, ManifestEntry]) -> None:
    prefixes = {
        "data/fixtures/dem_glo30/",
        "data/fixtures/sentinel2/",
        "data/fixtures/asf/",
        "data/fixtures/cdse/",
    }
    for prefix in prefixes:
        assert any(p.startswith(prefix) for p in latest_by_path), f"no ledger entry under {prefix}"


def test_every_fixture_entry_matches_its_bytes(
    repo_root: Path, latest_by_path: dict[str, ManifestEntry]
) -> None:
    for path, e in latest_by_path.items():
        file = repo_root / path
        assert file.is_file(), f"{path} recorded but missing"
        assert e.status in (ManifestStatus.fetched, ManifestStatus.listed), (path, e.status)
        assert e.sha256 == sha256_of_file(file), f"sha256 mismatch for {path}"
        assert e.size_bytes == file.stat().st_size, f"size mismatch for {path}"
        assert e.provenance is Provenance.real
        assert e.url and e.url.startswith("https://"), path
        assert e.licence_source_url and e.licence_source_url.startswith("https://"), path
        assert e.retrieved_at is not None and e.bbox_4326 is not None, path
        assert e.adapter and e.adapter_version, path


def test_every_fixture_file_is_recorded(
    fixtures_dir: Path, repo_root: Path, latest_by_path: dict[str, ManifestEntry]
) -> None:
    unrecorded = [
        p.relative_to(repo_root).as_posix()
        for p in fixtures_dir.rglob("*")
        if p.is_file()
        and is_eo_fixture(p.relative_to(repo_root).as_posix())
        and p.name not in DOC_FILES
        and not p.name.endswith(".md")
        and p.relative_to(repo_root).as_posix() not in latest_by_path
    ]
    assert unrecorded == [], f"files under data/fixtures without a ledger entry: {unrecorded}"


def test_fixtures_md_agrees_with_ledger(
    fixtures_dir: Path, latest_by_path: dict[str, ManifestEntry]
) -> None:
    md = fixtures_dir / "FIXTURES.md"
    assert md.exists(), "data/fixtures/FIXTURES.md is missing"
    rows = [m for m in (ROW.match(line) for line in md.read_text("utf-8").splitlines()) if m]
    assert rows, "FIXTURES.md has no fixture rows"
    listed = {m["path"] for m in rows}
    for m in rows:
        e = latest_by_path.get(m["path"])
        assert e is not None, f"FIXTURES.md row without ledger entry: {m['path']}"
        assert e.sha256 == m["sha"] and e.size_bytes == int(m["size"]), m["path"]
        assert e.url == m["url"], m["path"]
    eo_paths = {p for p, e in latest_by_path.items() if e.adapter == "fixture-fetch"}
    assert eo_paths <= listed, f"EO fixtures missing from FIXTURES.md: {sorted(eo_paths - listed)}"


def test_nothing_synthetic_under_data(entries: list[ManifestEntry]) -> None:
    for e in entries:
        if e.path is not None and e.path.startswith("data/"):
            assert e.provenance is not Provenance.synthetic, e.path
            assert e.status is not ManifestStatus.synthetic, e.path


def test_eo_fixture_budget(repo_root: Path, latest_by_path: dict[str, ManifestEntry]) -> None:
    """Keep the committed EO fixtures small; the ratchet is generous, the trend is what matters."""
    total = sum(
        (repo_root / p).stat().st_size
        for p, e in latest_by_path.items()
        if e.adapter == "fixture-fetch"
    )
    assert total < 2_500_000, f"EO fixtures total {total:,} B"
