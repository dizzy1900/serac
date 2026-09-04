"""The end-to-end lane and its validation suite, on the committed fixtures.

These assertions encode the as-run result of Prompt 2, not a hope about it: on both events the
chain stops before a forecast exists, and it must keep saying so. If a later change makes a
chain reach the CAP stage, these tests fail and someone has to look at why -- which is the
right outcome either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serac.cascade.evidence import Execution, StageOutcome
from serac.pipelines.e2e import CHAIN_STAGES, EVENTS, E2EError, run_e2e
from serac.validation.e2e import SUITE_NAME, run_suite

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def chamoli(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> object:
    return run_e2e(
        repo_root, "chamoli-2021", reports_dir=tmp_path_factory.mktemp("e2e-chamoli"), write=True
    )


@pytest.fixture(scope="module")
def langtang(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> object:
    return run_e2e(
        repo_root,
        "langtang-lhende-2026",
        reports_dir=tmp_path_factory.mktemp("e2e-langtang"),
        write=True,
    )


def test_an_unknown_event_is_refused(repo_root: Path) -> None:
    with pytest.raises(E2EError, match="unknown e2e event"):
        run_e2e(repo_root, "not-an-event", write=False)


@pytest.mark.parametrize("event_id", sorted(EVENTS))
def test_every_chain_stage_reports_an_outcome(repo_root: Path, event_id: str) -> None:
    result = run_e2e(repo_root, event_id, write=False)
    assert [s.stage for s in result.stages] == list(CHAIN_STAGES)
    assert result.completed, "no stage may be 'unavailable': the chain must run to its end"


@pytest.mark.parametrize("event_id", sorted(EVENTS))
def test_the_chain_produces_no_forecast_and_no_cap_message(repo_root: Path, event_id: str) -> None:
    """The as-run result. A CAP message here would mean something upstream stopped refusing."""
    result = run_e2e(repo_root, event_id, write=False)
    assert result.cap_identifier is None
    cap = result.stage("cap")
    runout = result.stage("runout")
    assert cap is not None and cap.outcome is StageOutcome.not_reached
    assert runout is not None and runout.outcome is StageOutcome.not_reached
    assert "No substitute input was used" in " ".join(cap.notes)


@pytest.mark.parametrize("event_id", sorted(EVENTS))
def test_the_detector_does_not_fire_on_the_two_receiver_fixtures(
    repo_root: Path, event_id: str
) -> None:
    result = run_e2e(repo_root, event_id, write=False)
    detection = result.stage("detection")
    assert detection is not None
    assert detection.execution is Execution.executed
    assert detection.outcome is StageOutcome.did_not_fire
    assert detection.measured["receivers_in_fixture"]
    assert len(detection.measured["receivers_in_fixture"]) < 3


@pytest.mark.parametrize("event_id", sorted(EVENTS))
def test_m2_refuses_and_the_refusal_text_reaches_the_report(repo_root: Path, event_id: str) -> None:
    result = run_e2e(repo_root, event_id, write=False)
    lfh = result.stage("lfh")
    assert lfh is not None
    assert lfh.outcome is StageOutcome.refused
    assert lfh.summary.startswith("REFUSED")
    assert lfh.measured["mass"] is None
    assert lfh.measured["status"] == "failed"


def test_chamoli_has_no_frozen_ensemble_so_the_loss_stage_is_not_reached(
    repo_root: Path,
) -> None:
    result = run_e2e(repo_root, "chamoli-2021", write=False)
    loss_stage = result.stage("avoided_loss")
    assert loss_stage is not None and loss_stage.outcome is StageOutcome.not_reached
    assert result.loss is None
    assert any("No frozen runout ensemble exists" in c for c in result.caveats)


def test_langtang_runs_the_loss_engine_on_the_prior_and_costs_nothing(repo_root: Path) -> None:
    result = run_e2e(repo_root, "langtang-lhende-2026", write=False)
    loss_stage = result.stage("avoided_loss")
    assert loss_stage is not None
    assert loss_stage.outcome is StageOutcome.insufficient_input
    assert result.loss is not None
    assert result.loss.determined_asset_ids == []
    assert len(result.loss.undetermined) == 14
    assert any("out of band with the chain" in c for c in result.caveats)


def test_the_reports_carry_the_refusal_and_the_stopping_point(
    repo_root: Path, tmp_path: Path
) -> None:
    result = run_e2e(repo_root, "langtang-lhende-2026", reports_dir=tmp_path, write=True)
    markdown = (tmp_path / "langtang-lhende-2026.md").read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "langtang-lhende-2026.json").read_text(encoding="utf-8"))

    assert "The chain stops at the `detection` stage" in markdown
    assert "REFUSED: only 3 station(s) contributed" in markdown
    assert "**no cascade forecast and no CAP alert**" in markdown
    assert "INPUT PROVENANCE" in markdown
    assert payload["stopped_at"] == result.stopped_at
    assert payload["chain_completed"] is True
    assert payload["cap_identifier"] is None
    assert len(payload["stages"]) == len(CHAIN_STAGES)
    assert payload["avoided_loss_response"]["status"] == "not_implemented"


def test_the_validation_suite_passes_and_records_the_early_stops(repo_root: Path) -> None:
    result = run_suite(repo_root)
    assert result.suite == SUITE_NAME
    assert result.passed, [c.name for c in result.checks if c.failed]

    names = {c.name for c in result.checks}
    assert "latency_report_generated" in names
    assert "cap_signed_validates" in names
    assert "cap_signature_verifies" in names
    assert "avoided_loss_computed_response_validates" in names

    warnings = [c for c in result.checks if not c.ok and c.severity.value == "warning"]
    assert any("chain_produced_a_forecast" in c.name for c in warnings), (
        "the suite must record that the chain produced no forecast"
    )
    latency = json.loads(
        (repo_root / "reports" / "e2e" / "latency.json").read_text(encoding="utf-8")
    )
    assert set(latency["events"]) == set(EVENTS)
    for event in latency["events"].values():
        assert event["cap_generation"]["emitted"] is False
