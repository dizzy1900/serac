"""`make validate-ingest`: manifest integrity, checksums re-hashed, no NISAR level mixing.

Every rule is a named check on `data/manifest.jsonl` and the files it points at. Rows whose
bytes live under DVC-tracked directories that have not been pulled are reported, not failed;
rows that point at committed fixtures must re-hash exactly.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from serac.adapters.eo.nisar_constraints import NisarLevel
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "ingest"
SYNTHETIC_PREFIX = "tests/fixtures/synthetic/"
FIXTURES_PREFIX = "data/fixtures/"
DVC_PREFIXES = ("data/raw/", "data/interim/", "data/features/")
DOC_NAMES = {"FIXTURES.md", ".gitkeep", "README.md", "manifest.json"}
"""`manifest.json` is the seismic lane's per-directory index, not a fixture (see FIXTURES.md)."""


def _latest_by_path(entries: list[ManifestEntry]) -> dict[str, ManifestEntry]:
    latest: dict[str, ManifestEntry] = {}
    for e in entries:
        if e.path is None:
            continue
        prev = latest.get(e.path)
        if prev is None or e.recorded_at >= prev.recorded_at:
            latest[e.path] = e
    return latest


def run_suite(repo: Path) -> SuiteResult:
    suite = Suite(SUITE_NAME, repo)
    ledger_path = repo / "data" / "manifest.jsonl"
    if not suite.check("ingest.ledger_exists", ledger_path.exists(), str(ledger_path)):
        return suite.result()
    try:
        entries = list(JsonlManifestLedger(ledger_path).entries())
    except ValueError as exc:
        suite.check("ingest.ledger_parses", False, str(exc))
        return suite.result()
    suite.check("ingest.ledger_parses", True, f"{len(entries)} rows")
    counts = Counter(f"{e.source.value}/{e.status.value}" for e in entries)
    suite.info(
        "ingest.rows_by_source_status", ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )

    latest = _latest_by_path(entries)
    _check_rehash(suite, repo, latest)
    _check_not_fetched(suite, repo, entries)
    _check_synthetic_boundaries(suite, repo, entries)
    _check_nisar_levels(suite, entries)
    _check_fixture_files_recorded(suite, repo, latest)
    return suite.result()


def _check_rehash(suite: Suite, repo: Path, latest: dict[str, ManifestEntry]) -> None:
    mismatched: list[str] = []
    checked = 0
    missing_fixture: list[str] = []
    unpulled = 0
    for path, e in latest.items():
        if e.status not in (
            ManifestStatus.fetched,
            ManifestStatus.listed,
            ManifestStatus.synthetic,
        ):
            continue
        file = repo / path
        if not file.exists():
            if path.startswith(DVC_PREFIXES):
                unpulled += 1
            else:
                missing_fixture.append(path)
            continue
        checked += 1
        if e.sha256 is None or e.size_bytes is None:
            mismatched.append(f"{path}: no sha256/size recorded")
            continue
        if file.stat().st_size != e.size_bytes or sha256_of_file(file) != e.sha256:
            mismatched.append(f"{path}: bytes differ from the ledger")
    suite.check(
        "ingest.fetched_rows_rehash",
        not mismatched,
        "; ".join(mismatched) if mismatched else f"{checked} file(s) re-hashed and matching",
    )
    suite.check(
        "ingest.committed_files_present",
        not missing_fixture,
        "; ".join(missing_fixture) if missing_fixture else "every committed path exists",
    )
    if unpulled:
        suite.info("ingest.dvc_paths_not_pulled", f"{unpulled} DVC-tracked path(s) absent locally")


def _check_not_fetched(suite: Suite, repo: Path, entries: list[ManifestEntry]) -> None:
    offenders = [
        f"{e.source.value}/{e.product_id}: {e.path}"
        for e in entries
        if e.status in (ManifestStatus.not_fetched, ManifestStatus.requested, ManifestStatus.failed)
        and e.path is not None
        and (repo / e.path).exists()
    ]
    suite.check(
        "ingest.not_fetched_rows_have_no_file",
        not offenders,
        "; ".join(offenders) or "no not_fetched/requested/failed row points at an existing file",
    )
    unhashed = [
        f"{e.source.value}/{e.product_id}"
        for e in entries
        if e.status is ManifestStatus.fetched and (e.sha256 is None or e.retrieved_at is None)
    ]
    suite.check("ingest.fetched_rows_carry_provenance", not unhashed, "; ".join(unhashed) or "ok")


def _check_synthetic_boundaries(suite: Suite, repo: Path, entries: list[ManifestEntry]) -> None:
    real_in_synthetic = [
        e.path
        for e in entries
        if e.path
        and e.path.startswith(SYNTHETIC_PREFIX)
        and e.provenance is not Provenance.synthetic
    ]
    suite.check(
        "ingest.no_real_row_under_synthetic_dir",
        not real_in_synthetic,
        "; ".join(map(str, real_in_synthetic)) or "ok",
    )
    synthetic_in_data = [
        e.path
        for e in entries
        if e.path
        and e.path.startswith("data/")
        and (e.provenance is Provenance.synthetic or e.status is ManifestStatus.synthetic)
    ]
    suite.check(
        "ingest.no_synthetic_under_data",
        not synthetic_in_data,
        "; ".join(map(str, synthetic_in_data)) or "ok",
    )
    synthetic_rows = [e for e in entries if e.provenance is Provenance.synthetic]
    unlabelled = [
        f"{e.source.value}/{e.product_id}"
        for e in synthetic_rows
        if not e.notes
        or "synthetic" not in e.notes.lower()
        or e.status is not ManifestStatus.synthetic
    ]
    suite.check(
        "ingest.synthetic_rows_labelled",
        not unlabelled,
        "; ".join(unlabelled) or f"{len(synthetic_rows)} synthetic row(s), all labelled",
    )
    synthetic_dir = repo / SYNTHETIC_PREFIX
    if synthetic_dir.exists():
        recorded = {e.path for e in synthetic_rows if e.path}
        stray = [
            p.relative_to(repo).as_posix()
            for p in synthetic_dir.rglob("*")
            if p.is_file()
            and p.name not in DOC_NAMES
            and p.relative_to(repo).as_posix() not in recorded
        ]
        suite.check("ingest.synthetic_files_recorded", not stray, "; ".join(stray) or "ok")


def _check_nisar_levels(suite: Suite, entries: list[ManifestEntry]) -> None:
    per_aoi: dict[str, set[str]] = defaultdict(set)
    unknown: list[str] = []
    for e in entries:
        if e.source is not DataSource.nisar_asf or e.status is not ManifestStatus.fetched:
            continue
        level = (e.product_level or "").lower()
        if level not in (NisarLevel.beta.value, NisarLevel.provisional.value):
            unknown.append(f"{e.aoi_id}/{e.product_id}: level {e.product_level!r}")
        per_aoi[e.aoi_id or "?"].add(level)
    mixed = [f"{aoi}: {sorted(levels)}" for aoi, levels in per_aoi.items() if len(levels) > 1]
    suite.check(
        "ingest.nisar_levels_not_mixed",
        not mixed and not unknown,
        "; ".join(mixed + unknown)
        or f"{sum(len(v) for v in per_aoi.values())} fetched NISAR level(s), none mixed",
    )


def _check_fixture_files_recorded(
    suite: Suite, repo: Path, latest: dict[str, ManifestEntry]
) -> None:
    fixtures = repo / FIXTURES_PREFIX
    if not fixtures.exists():
        suite.warn("ingest.fixture_files_recorded", False, f"no {FIXTURES_PREFIX}")
        return
    unrecorded = [
        p.relative_to(repo).as_posix()
        for p in fixtures.rglob("*")
        if p.is_file()
        and p.name not in DOC_NAMES
        and not p.name.endswith(".md")
        and p.relative_to(repo).as_posix() not in latest
    ]
    suite.check(
        "ingest.fixture_files_recorded",
        not unrecorded,
        "; ".join(unrecorded) or "every fixture file has a ledger row",
    )
