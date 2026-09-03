"""The stream and contracts validation suites pass on the committed tree, offline."""

from __future__ import annotations

import json
from pathlib import Path

from serac.validation import contracts, stream
from serac.validation.result import Severity, load_report, write_report


def test_stream_suite_passes_offline(repo_root: Path, tmp_path: Path) -> None:
    result = stream.run_suite(repo_root, tmp_path / "replay")
    failed = [c for c in result.checks if c.failed]
    assert failed == [], "\n".join(f"{c.name}: {c.details}" for c in failed)
    names = {c.name for c in result.checks}
    assert {
        "detector_docstring_stub_marker",
        "cap_xsd_vendor_manifest",
        "fixture_hashes:chamoli-2021",
        "replay_completes:chamoli-2021",
        "replay_pending_zero:chamoli-2021",
        "replay_consumed_all:chamoli-2021",
        "replay_report_validates:chamoli-2021",
        "golden_ratios:chamoli-2021",
        "synthetic_lane_detects",
        "synthetic_lane_caps",
        "synthetic_cap_validates_xsd",
    } <= names
    # Firing on real fixtures is an info observation, never an error/warning.
    fired = [c for c in result.checks if c.name.startswith("stub_fired_observation:")]
    assert fired and all(c.severity == Severity.info for c in fired)
    assert (tmp_path / "replay" / "chamoli-2021.json").exists()
    assert (tmp_path / "replay" / "synthetic-lp-burst.json").exists()
    path = write_report(result, tmp_path / "validation")
    assert load_report(path).status == "passed"


def test_stream_suite_warns_when_optional_fixture_missing(repo_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for rel in (
        "data/fixtures/seismic/chamoli-2021",
        "contracts/vendor/cap",
        "src/serac/streaming",
        "contracts",
        "tests/fixtures/golden",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    src_fix = repo_root / "data" / "fixtures" / "seismic" / "chamoli-2021"
    for f in src_fix.iterdir():
        (root / "data/fixtures/seismic/chamoli-2021" / f.name).write_bytes(f.read_bytes())
    for f in (repo_root / "contracts" / "vendor" / "cap").iterdir():
        (root / "contracts/vendor/cap" / f.name).write_bytes(f.read_bytes())
    (root / "contracts" / "replay-report.v0.json").write_bytes(
        (repo_root / "contracts" / "replay-report.v0.json").read_bytes()
    )
    (root / "src/serac/streaming/detector_stub.py").write_bytes(
        (repo_root / "src/serac/streaming/detector_stub.py").read_bytes()
    )
    golden = repo_root / "tests/fixtures/golden/detector_stub_chamoli-2021.json"
    (root / "tests/fixtures/golden" / golden.name).write_bytes(golden.read_bytes())

    result = stream.run_suite(root, tmp_path / "replay")
    assert result.passed
    warn = next(c for c in result.checks if c.name == "fixture_hashes:langtang-2026")
    assert warn.severity == Severity.warning and not warn.ok
    assert not any(c.name.startswith("replay_completes:langtang") for c in result.checks)


def test_stream_suite_fails_on_fixture_drift(repo_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for rel in (
        "data/fixtures/seismic/chamoli-2021",
        "contracts/vendor/cap",
        "src/serac/streaming",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    src_fix = repo_root / "data" / "fixtures" / "seismic" / "chamoli-2021"
    for f in src_fix.iterdir():
        (root / "data/fixtures/seismic/chamoli-2021" / f.name).write_bytes(f.read_bytes())
    (root / "data/fixtures/seismic/chamoli-2021/stations.xml").write_bytes(b"<tampered/>")
    for f in (repo_root / "contracts" / "vendor" / "cap").iterdir():
        (root / "contracts/vendor/cap" / f.name).write_bytes(f.read_bytes())
    result = stream.run_suite(root, tmp_path / "replay")
    assert not result.passed
    bad = next(c for c in result.checks if c.name == "fixture_hashes:chamoli-2021")
    assert bad.failed and "sha256" in bad.details


def test_contracts_suite_passes(repo_root: Path) -> None:
    result = contracts.run_suite(repo_root)
    assert result.passed, result.summary()
    assert any(c.name == "schema_export_check" and c.ok for c in result.checks)
    assert any(c.name == "schema_valid:replay-report" for c in result.checks)


def test_contracts_suite_detects_drift_and_invalid_schema(repo_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    for f in (repo_root / "contracts").glob("*.v0.json"):
        (root / "contracts" / f.name).write_bytes(f.read_bytes())
    target = root / "contracts" / "replay-report.v0.json"
    doc = json.loads(target.read_text())
    doc["type"] = 42  # not a valid JSON Schema
    target.write_text(json.dumps(doc))
    result = contracts.run_suite(root)
    assert not result.passed
    assert next(c for c in result.checks if c.name == "schema_export_check").failed
    assert next(c for c in result.checks if c.name == "schema_valid:replay-report").failed
