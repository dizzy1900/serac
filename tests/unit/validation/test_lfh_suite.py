"""validate-lfh: passes on the committed tree, and fails for the right reason when tampered.

A gate that only ever passes proves nothing. Each test here breaks exactly one thing and
asserts that exactly the corresponding check fails -- a fabricated reproduction, a reference
that was never fetched, a tampered fixture, a config changed after the seal, a refusal that
quietly kept its location.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from serac.models.lfh.config import LfhConfig, seal_config, write_seal
from serac.models.lfh.references import load_references, write_references
from serac.validation.lfh import compare, run_suite
from serac.validation.result import SuiteResult


def failed(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if c.failed}


def warned(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if not c.ok and c.severity == "warning"}


@pytest.fixture
def tree(repo_root: Path, tmp_path: Path) -> Path:
    """A copy of everything validate-lfh reads, so a test can tamper with it freely."""
    fake = tmp_path / "repo"
    (fake / "data").mkdir(parents=True)
    shutil.copytree(repo_root / "data" / "fixtures", fake / "data" / "fixtures")
    shutil.copytree(repo_root / "data" / "references", fake / "data" / "references")
    shutil.copy(repo_root / "data" / "manifest.jsonl", fake / "data" / "manifest.jsonl")
    shutil.copytree(repo_root / "reports" / "m2", fake / "reports" / "m2")
    shutil.copytree(repo_root / "src", fake / "src")
    return fake


def _run(tree: Path, target_id: str) -> dict:
    return json.loads((tree / "reports" / "m2" / f"{target_id}.json").read_text(encoding="utf-8"))


def _write_run(tree: Path, target_id: str, payload: dict) -> None:
    (tree / "reports" / "m2" / f"{target_id}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def test_passes_on_the_committed_tree(tree: Path) -> None:
    result = run_suite(tree)
    assert result.passed, [c for c in result.checks if c.failed]
    assert not warned(result), [c for c in result.checks if not c.ok]


def test_the_three_reproductions_are_the_ones_that_pass(repo_root: Path) -> None:
    """The headline claim, asserted as a table rather than a summary line."""
    references = load_references(repo_root)
    rows = {
        target.target_id: compare(
            target,
            json.loads(
                (repo_root / "reports" / "m2" / f"{target.target_id}.json").read_text(
                    encoding="utf-8"
                )
            ),
        )
        for target in references.reproductions
    }
    overlapping = {k for k, v in rows.items() if v.status == "computed" and v.overlaps}
    assert overlapping == {
        "bingham-canyon-2013-1",
        "taan-fiord-2015",
        "lamplugh-glacier-2016",
    }
    assert rows["chamoli-2021"].status == "failed", (
        "Chamoli must refuse: its event window is quieter than its pre-event noise"
    )
    for target_id in overlapping:
        assert rows[target_id].sanity_ok, (
            f"{target_id} overlaps but its median is outside the magnitude band, which means "
            "the overlap came from interval width rather than agreement"
        )


def test_fewer_than_three_fetched_references_fails_the_gate(tree: Path) -> None:
    """The rule that cannot be softened: two is not enough, and memory is never enough."""
    references = load_references(tree)
    stripped = references.model_copy(
        update={
            "sources": [
                source
                if index < 2
                else source.model_copy(update={"doi": None, "doi_resolved_via": None})
                for index, source in enumerate(references.sources)
            ]
        }
    )
    write_references(stripped, tree)
    result = run_suite(tree)
    assert "lfh.published_refs_fetched" in failed(result)
    detail = next(c for c in result.checks if c.name == "lfh.published_refs_fetched").details
    assert "published_refs_fetched=False" in detail


def test_a_narrow_wrong_answer_fails_the_overlap_check(tree: Path) -> None:
    payload = _run(tree, "bingham-canyon-2013-1")
    mass = payload["force_history"]["mass"]
    mass["mass_kg_p05"], mass["mass_kg_p50"], mass["mass_kg_p95"] = 1e5, 2e5, 3e5
    _write_run(tree, "bingham-canyon-2013-1", payload)
    result = run_suite(tree)
    assert "lfh.reproductions_overlap" in failed(result)


def test_a_vacuously_wide_interval_warns_even_though_it_overlaps(tree: Path) -> None:
    """Overlap alone is not evidence. A median far from the published centre must be flagged."""
    payload = _run(tree, "bingham-canyon-2013-1")
    mass = payload["force_history"]["mass"]
    mass["mass_kg_p05"] = 1.0e6
    mass["mass_kg_p50"] = 1.0e14
    mass["mass_kg_p95"] = 1.0e18
    _write_run(tree, "bingham-canyon-2013-1", payload)
    result = run_suite(tree)
    assert "lfh.reproductions_overlap" not in failed(result), "it does still overlap"
    warning = next(c for c in result.checks if c.name == "lfh.magnitude_sanity")
    assert not warning.ok and not warning.failed, "a warning, not an error"
    assert "width of serac's interval" in warning.details


def test_a_point_mass_in_a_committed_run_is_caught(tree: Path) -> None:
    payload = _run(tree, "bingham-canyon-2013-1")
    mass = payload["force_history"]["mass"]
    mass["mass_kg_p05"] = mass["mass_kg_p95"] = mass["mass_kg_p50"]
    _write_run(tree, "bingham-canyon-2013-1", payload)
    result = run_suite(tree)
    assert "lfh.no_point_mass" in failed(result)


def test_a_refusal_that_kept_its_location_is_caught(tree: Path) -> None:
    """The whole point of refusing is not publishing a location."""
    payload = _run(tree, "langtang-lhende-2026")
    payload["force_history"]["source_location"] = {
        "latitude": 28.271,
        "longitude": 85.515,
        "depth_km": 1.0,
        "uncertainty_radius_km": 2.0,
        "method": "gsf_grid_search",
        "grid_spacing_km": 2.0,
        "variance_reduction": 0.9,
        "azimuthal_gap_deg": 317.0,
        "source_refs": [],
    }
    _write_run(tree, "langtang-lhende-2026", payload)
    result = run_suite(tree)
    assert failed(result) & {"lfh.no_point_mass", "lfh.refusals_state_their_geometry"}


def test_a_tampered_greens_fixture_is_caught(tree: Path) -> None:
    fixture = next((tree / "data" / "fixtures" / "greens" / "lfh").rglob("*.json.gz"))
    payload = json.loads(gzip.decompress(fixture.read_bytes()))
    payload["provider"] = "tampered"
    fixture.write_bytes(gzip.compress(json.dumps(payload).encode()))
    result = run_suite(tree)
    assert "lfh.greens_fixture_hashes" in failed(result)


def test_a_tampered_waveform_fixture_is_caught(tree: Path) -> None:
    fixture = next((tree / "data" / "fixtures" / "lfh").rglob("*.mseed"))
    fixture.write_bytes(fixture.read_bytes() + b"\x00" * 512)
    result = run_suite(tree)
    assert "lfh.waveforms_fixture_hashes" in failed(result)


def test_an_unledgered_fixture_is_caught(tree: Path) -> None:
    (tree / "data" / "fixtures" / "greens" / "lfh" / "prem_a_20s" / "smuggled.json.gz").write_bytes(
        gzip.compress(b"{}")
    )
    result = run_suite(tree)
    assert "lfh.greens_fixture_hashes" in failed(result)


def test_a_run_under_a_different_config_is_caught(tree: Path) -> None:
    """The anti-tuning check: a knob turned between the reproductions and a new event."""
    payload = _run(tree, "blatten-2025")
    payload["config_hash"] = "0" * 64
    _write_run(tree, "blatten-2025", payload)
    result = run_suite(tree)
    assert "lfh.runs_share_the_sealed_config" in failed(result)


def test_a_seal_that_does_not_match_the_working_config_is_caught(tree: Path) -> None:
    tweaked = LfhConfig().model_copy(update={"source_duration_s": 400.0})
    write_seal(seal_config(tweaked, git_sha=None, reproductions=[]), tree)
    result = run_suite(tree)
    assert "lfh.seal_matches_current_config" in failed(result)


def test_a_missing_seal_fails(tree: Path) -> None:
    (tree / "reports" / "m2" / "seal.json").unlink()
    result = run_suite(tree)
    assert "lfh.seal_present" in failed(result)


def test_a_new_event_report_without_a_disagreement_section_is_caught(tree: Path) -> None:
    report = tree / "reports" / "m2" / "langtang-lhende-2026.md"
    report.write_text(
        report.read_text(encoding="utf-8").replace("## Disagreement", "## Notes"),
        encoding="utf-8",
    )
    result = run_suite(tree)
    assert "lfh.new_events_report_disagreement" in failed(result)


def test_importing_greens_into_the_streaming_layer_is_caught(tree: Path) -> None:
    """Green's functions must stay out of the bus: a modelled trace cannot be told from a
    recording once it is a `SeismicTrace` (ADR-0016)."""
    module = tree / "src" / "serac" / "streaming" / "detector_stub.py"
    module.write_text(
        "from serac.models.lfh.pipeline import invert_event\n" + module.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = run_suite(tree)
    assert "lfh.greens_never_published_on_the_bus" in failed(result)


def test_a_missing_reference_file_fails_cleanly(tmp_path: Path) -> None:
    result = run_suite(tmp_path)
    assert "lfh.references_load" in failed(result)
    assert len(result.checks) == 1, "the suite must stop rather than cascade"
