"""`validate-events` on fictional tmp repositories: each check passes and fails on cue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from serac.pipelines.events_index import build_index
from serac.pipelines.sources import dump_record
from serac.validation.events import load_transect_ids, run_suite
from serac.validation.result import Check, Severity, SuiteResult

if TYPE_CHECKING:
    from tests.unit.conftest import Fictional


def _check(result: SuiteResult, name: str) -> Check:
    matches = [c for c in result.checks if c.name == name]
    assert len(matches) == 1, f"{name!r} not found once in {[c.name for c in result.checks]}"
    return matches[0]


def _failed_names(result: SuiteResult) -> list[str]:
    return [c.name for c in result.checks if c.failed]


def _transects_geojson(*ids: str) -> str:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": [[2.0, 1.0], [2.1, 1.1]]},
                    "properties": {"id": i, "name": f"fictional transect {i}"},
                }
                for i in ids
            ],
        }
    )


def test_complete_repo_passes(tmp_path: Path, fictional: Fictional) -> None:
    result = run_suite(fictional.repo(tmp_path))
    assert result.passed, _failed_names(result)
    assert result.suite == "events"
    names = [c.name for c in result.checks]
    for expected in (
        "record:test-target",
        "record:test-negative",
        "record:test-co-seismic",
        "record:test-counterfactual",
        "index: events.parquet exists",
        "index: up to date",
        "test-target: every range sourced",
        "test-target: no best without qualifying source",
        "test-target: press-only ranges carry best=null",
        "test-target: every source sha256 in ledger",
        "roles: exactly one negative_control (moraine_collapse_glof)",
        "roles: at least one evacuation_counterfactual",
        "roles: at least one co_seismic_reference",
        "roles: exactly one target",
        "roles: target source_volume_m3 is null",
    ):
        assert expected in names, expected
    assert all(c.ok for c in result.checks)


def test_no_records(tmp_path: Path) -> None:
    (tmp_path / "data" / "events").mkdir(parents=True)
    result = run_suite(tmp_path)
    assert not result.passed
    assert [c.name for c in result.checks] == ["no records"]


def test_missing_negative_control(tmp_path: Path, fictional: Fictional) -> None:
    records = [r for r in fictional.library() if r["role"] != "negative_control"]
    result = run_suite(fictional.repo(tmp_path, records))
    assert _failed_names(result) == ["roles: exactly one negative_control (moraine_collapse_glof)"]


def test_missing_counterfactual_and_co_seismic(tmp_path: Path, fictional: Fictional) -> None:
    records = [r for r in fictional.library() if r["role"] in ("target", "negative_control")]
    result = run_suite(fictional.repo(tmp_path, records))
    assert set(_failed_names(result)) == {
        "roles: at least one evacuation_counterfactual",
        "roles: at least one co_seismic_reference",
    }


def test_two_targets_and_target_with_volume(tmp_path: Path, fictional: Fictional) -> None:
    records = fictional.library()
    records.append(fictional.event("test-target-2", role="target"))
    result = run_suite(fictional.repo(tmp_path, records))
    assert _failed_names(result) == ["roles: exactly one target"]

    volume = {"low": 1.0, "high": 2.0, "unit": "m3", "source_refs": ["test-src-1"]}
    records = fictional.library()
    records[0]["source_volume_m3"] = volume
    del records[0]["field_notes"]["source_volume_m3"]
    records[0]["sources"][0]["claims_supported"].append("source_volume_m3")
    result = run_suite(fictional.repo(tmp_path / "b", records))
    assert _failed_names(result) == ["roles: target source_volume_m3 is null"]


def test_sha256_not_in_ledger(tmp_path: Path, fictional: Fictional) -> None:
    records = fictional.library()
    records[1]["sources"][0]["sha256"] = fictional.sha_c
    result = run_suite(fictional.repo(tmp_path, records))
    assert _failed_names(result) == ["test-negative: every source sha256 in ledger"]
    assert "test-src-1" in _check(result, "test-negative: every source sha256 in ledger").details


def test_missing_and_stale_index(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path, index=False)
    result = run_suite(repo)
    assert _failed_names(result) == ["index: events.parquet exists"]
    assert "index: up to date" not in [c.name for c in result.checks]

    build_index(repo / "data" / "events")
    path = repo / "data" / "events" / "test-target.json"
    record = fictional.read(path)
    record["notes"] = "edited after the index was built"
    path.write_text(dump_record(record), encoding="utf-8")
    result = run_suite(repo)
    assert _failed_names(result) == ["index: up to date"]


def test_invalid_record_file_is_named(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path)
    broken = repo / "data" / "events" / "test-broken.json"
    broken.write_text('{"event_id": "test-broken"}', encoding="utf-8")
    result = run_suite(repo)
    failed = _failed_names(result)
    assert "record:test-broken" in failed
    assert "index: up to date" in failed, "a new file also makes the index stale"
    assert "sources" in _check(result, "record:test-broken").details


def test_press_only_best_is_an_error(tmp_path: Path, fictional: Fictional) -> None:
    records = fictional.library()
    records[0]["sources"][0]["kind"] = "press_report"
    records[0]["sources"][0]["peer_reviewed"] = False
    # The index cannot be built over an invalid record; the suite must still name the defect.
    result = run_suite(fictional.repo(tmp_path, records, index=False))
    check = _check(result, "record:test-target")
    assert check.failed
    assert "best requires a source of kind" in check.details


def test_transects_resolve_in_aoi(tmp_path: Path, fictional: Fictional) -> None:
    observation = {
        "transect_id": "test-transect-1",
        "description": "fictional stage rise, not measured",
        "source_refs": ["test-src-1"],
    }
    records = fictional.library()
    records[0]["aoi_id"] = "test-aoi"
    records[0]["transect_observations"] = [observation]
    repo = fictional.repo(tmp_path, records)
    name = "test-target: transects resolve in AOI"

    # AOI directory absent -> warning only, suite passes.
    result = run_suite(repo)
    assert result.passed
    check = _check(result, name)
    assert not check.ok and check.severity == Severity.warning
    assert "absent" in check.details

    # File exists without the id -> error.
    aoi_dir = repo / "data" / "aoi" / "test-aoi"
    aoi_dir.mkdir(parents=True)
    (aoi_dir / "transects.geojson").write_text(_transects_geojson("test-transect-other"))
    result = run_suite(repo)
    check = _check(result, name)
    assert check.failed
    assert "test-transect-1" in check.details

    # File exists with the id -> ok.
    (aoi_dir / "transects.geojson").write_text(
        _transects_geojson("test-transect-other", "test-transect-1")
    )
    result = run_suite(repo)
    assert _check(result, name).ok
    assert result.passed

    # Not a FeatureCollection -> error.
    (aoi_dir / "transects.geojson").write_text("[]")
    assert _check(run_suite(repo), name).failed
    assert load_transect_ids(aoi_dir / "transects.geojson") is None


def test_transects_without_aoi_id_warn(tmp_path: Path, fictional: Fictional) -> None:
    records = fictional.library()
    records[0]["transect_observations"] = [
        {
            "transect_id": "test-transect-1",
            "description": "fictional",
            "source_refs": ["test-src-1"],
        }
    ]
    result = run_suite(fictional.repo(tmp_path, records))
    assert result.passed
    check = _check(result, "test-target: transects resolve in AOI")
    assert check.severity == Severity.warning and not check.ok
    assert "no aoi_id" in check.details
