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
from serac.validation.lfh import agreement, compare, run_suite
from serac.validation.result import Severity, SuiteResult

#: What the committed tree is expected to leave unmet. The brief's criterion is reproduction
#: "within stated uncertainty"; on the committed runs only Bingham Canyon meets it, and
#: Taan Fiord's duration (296 s against a published 90 s) does not. Both are recorded here so
#: that a change which makes them pass -- or adds a third -- has to say so.
EXPECTED_UNMET = {
    "lfh.reproductions_within_stated_uncertainty",
    "lfh.duration_within_stated_uncertainty",
}


def failed(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if c.failed}


def unmet(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if not c.ok and c.severity == Severity.criterion_unmet}


def errored(result: SuiteResult) -> set[str]:
    return {c.name for c in result.checks if c.failed and c.severity == Severity.error}


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


def test_the_committed_tree_fails_only_on_criteria_of_the_brief(tree: Path) -> None:
    """Nothing in the suite is broken; two criteria of the brief are not met, and say so.

    This test used to assert the suite passed. It passed because the reproduction criterion
    was interval *intersection*, which two of the three reproductions clear while disagreeing
    with the published figure by more than the published uncertainty allows. The gate now
    applies the brief's criterion and is red, which is the honest state of M2.
    """
    result = run_suite(tree)
    assert unmet(result) == EXPECTED_UNMET, [c for c in result.checks if c.failed]
    assert not warned(result), [c for c in result.checks if not c.ok]
    # The committed reports/m2/seal.json records a config hash that is not the hash of the
    # config it carries (a config field changed after it was written), so the seal check
    # fails on the committed tree. That artefact is not this suite's to rewrite; what is
    # asserted here is that no *other* check errors and that the suite reports it rather than
    # aborting on the ValidationError.
    assert errored(result) <= {"lfh.seal_present"}, [c for c in result.checks if c.failed]


def test_an_unreadable_seal_is_a_finding_rather_than_a_crash(tree: Path) -> None:
    """A gate that raises on one bad artefact reports nothing at all about the other 22."""
    seal = tree / "reports" / "m2" / "seal.json"
    payload = json.loads(seal.read_text(encoding="utf-8"))
    payload["config_hash"] = "0" * 64
    seal.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    result = run_suite(tree)
    check = next(c for c in result.checks if c.name == "lfh.seal_present")
    assert not check.ok and check.severity == Severity.error
    assert "does not validate" in check.details
    assert len(result.checks) > 20, "the rest of the suite must still have run"


def test_only_one_reproduction_agrees_within_stated_uncertainty(repo_root: Path) -> None:
    """The headline claim, asserted as a table rather than a summary line.

    Three of four reproductions overlap; one agrees within stated uncertainty. The gap between
    those two numbers is the whole reason the criterion was strengthened.
    """
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
    agreeing = {k for k, v in rows.items() if v.within_stated_uncertainty}
    assert agreeing == {"bingham-canyon-2013-1"}, (
        "Taan Fiord (4.5e10 kg against a published 1.0-1.5e11) and Lamplugh (1.9e11 against "
        "1.34-1.41e11) overlap only through the width of serac's interval"
    )
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
    # The point of the strengthened criterion: the same vacuous interval fails outright
    # rather than merely warning, and it fails with the evidence attached.
    criterion = next(
        c for c in result.checks if c.name == "lfh.reproductions_within_stated_uncertainty"
    )
    assert not criterion.ok
    assert "serac's median is outside the published interval" in criterion.details


def test_the_unmet_criterion_is_not_reported_as_an_error(tree: Path) -> None:
    """`criterion_unmet` means the code worked and a criterion was missed. Not a defect."""
    result = run_suite(tree)
    criterion = next(
        c for c in result.checks if c.name == "lfh.reproductions_within_stated_uncertainty"
    )
    assert not criterion.ok
    assert criterion.severity == Severity.criterion_unmet
    assert criterion.failed, "an unmet criterion still fails the suite"
    assert "not a defect in the code" in criterion.details


def test_agreement_within_stated_uncertainty_can_be_met(tree: Path) -> None:
    """The criterion is not vacuously unreachable: three agreeing runs turn it green.

    Without this, a gate that always fails would be indistinguishable from one that measures
    something -- the same class of defect in the opposite direction as a gate that always
    passes.
    """
    for target_id, (p05, p50, p95) in {
        "taan-fiord-2015": (8.0e10, 1.2e11, 2.0e11),
        "lamplugh-glacier-2016": (1.0e11, 1.37e11, 1.8e11),
    }.items():
        payload = _run(tree, target_id)
        mass = payload["force_history"]["mass"]
        mass["mass_kg_p05"], mass["mass_kg_p50"], mass["mass_kg_p95"] = p05, p50, p95
        _write_run(tree, target_id, payload)
    result = run_suite(tree)
    criterion = next(
        c for c in result.checks if c.name == "lfh.reproductions_within_stated_uncertainty"
    )
    assert criterion.ok, criterion.details
    assert "3 of 4 meet it" in criterion.details


def test_a_published_duration_serac_contradicts_is_reported(tree: Path) -> None:
    """Taan Fiord's 296 s against a published 90 s was documented in prose and ungated."""
    check = next(
        c for c in run_suite(tree).checks if c.name == "lfh.duration_within_stated_uncertainty"
    )
    assert not check.ok and check.severity == Severity.criterion_unmet
    assert "published 90-90 s" in check.details


def test_a_published_point_with_no_uncertainty_is_compared_against_seracs_interval() -> None:
    """Higman's "about 2 x 10^11 N" states no uncertainty, so serac's must contain it.

    Requiring a point estimate to equal a published point is a criterion no measurement can
    meet, so the published-interval leg is dropped -- and that is stated in the evidence line
    rather than applied silently.
    """
    inside = agreement(2.0e11, 2.0e11, 2.0e11, 1.38e11, 1.73e11, 2.22e11)
    assert inside.ok and not inside.published_has_width
    assert "publication stated no uncertainty" in inside.reason()
    outside = agreement(90.0, 90.0, 90.0, 293.0, 296.0, 297.0)
    assert not outside.ok
    assert "published centre is outside serac's" in outside.reason()


def test_intersecting_intervals_do_not_count_as_agreement() -> None:
    """The exact gap this criterion closes, on the numbers that exposed it.

    Lamplugh Glacier: published 1.34-1.41e11 kg, serac 2.2e10 / 1.92e11 / 1.21e12. The
    intervals intersect; serac's median is 36 % above the top of the published interval.
    """
    clipping = agreement(1.34e11, 1.41e11, None, 2.2e10, 1.92e11, 1.21e12)
    assert not clipping.ok
    assert clipping.published_centre_inside_serac, "it does overlap -- that is the point"
    assert not clipping.median_inside_published
    # The published `best`, when one was printed, is the centre; otherwise the geometric mean,
    # which is the centre in the space a mass spanning decades actually varies in.
    assert agreement(1.0e10, 1.0e12, None, 1.0e10, 1.0e11, 1.0e12).published_centre == 1.0e11
    assert agreement(5.6e10, 8.4e10, 7.0e10, 1.6e10, 6.6e10, 2.7e11).published_centre == 7.0e10


def test_a_narrow_interval_that_excludes_the_published_centre_is_not_agreement() -> None:
    """The other half of mutual containment: a confident answer in the wrong place."""
    confident = agreement(1.0e11, 1.5e11, None, 1.01e11, 1.02e11, 1.03e11)
    assert confident.median_inside_published
    assert not confident.published_centre_inside_serac, "1.22e11 is outside 1.01-1.03e11"
    assert not confident.ok


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


def _reseal(tree: Path) -> None:
    """Put the copied tree under a seal that matches the working config.

    The committed `reports/m2/seal.json` records a config hash that the committed config no
    longer produces, so `_check_seal` stops at `seal_present` and the anti-tuning check below
    never runs. Re-stamping here keeps that test testing what its name says instead of
    passing on the stale artefact.
    """
    config_hash = LfhConfig().config_hash()
    for path in sorted((tree / "reports" / "m2").glob("*.json")):
        if path.name == "seal.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["config_hash"] = config_hash
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_seal(seal_config(LfhConfig(), git_sha=None, reproductions=[]), tree)


def test_a_run_under_a_different_config_is_caught(tree: Path) -> None:
    """The anti-tuning check: a knob turned between the reproductions and a new event."""
    _reseal(tree)
    assert "lfh.runs_share_the_sealed_config" not in failed(run_suite(tree)), (
        "a freshly sealed tree must not read as drifted, or the check below proves nothing"
    )
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


def test_the_published_bearing_check_uses_the_direction_of_motion(tree: Path) -> None:
    """Taan Fiord's published bearing is the strongest independent check M2 has.

    Nothing in the inversion is fitted to a direction, so a mirrored `F_north = -Ft` or a
    flipped transverse sign would move this conspicuously. The comparison is the force azimuth
    rotated by 180 degrees, because a slide pushes the ground opposite the way it travels.
    """
    result = run_suite(tree)
    check = next(c for c in result.checks if c.name == "lfh.runout_bearing_matches_published")
    assert check.ok
    assert "published 96 deg" in check.details
    assert "inside the interval" in check.details, (
        "a bearing inside its interval must not read as outside it -- `x or y - z` binds "
        "the subtraction to the wrong operand"
    )


def test_a_mirrored_bearing_is_caught(tree: Path) -> None:
    """What a flipped force convention would look like: 180 degrees out."""
    payload = _run(tree, "taan-fiord-2015")
    azimuth = payload["force_history"]["force_azimuth_deg"]
    for key in ("p05", "p50", "p95"):
        azimuth[key] = azimuth[key] + 180.0
    _write_run(tree, "taan-fiord-2015", payload)
    result = run_suite(tree)
    check = next(c for c in result.checks if c.name == "lfh.runout_bearing_matches_published")
    assert not check.ok
    assert "from the published one" in check.details
