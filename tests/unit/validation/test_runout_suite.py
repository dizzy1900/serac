"""`validate-runout` must fail loudly on the things it exists to catch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from serac.models.runout.ensemble import EnsembleDesign, write_frozen
from serac.models.runout.params import SOLVER_VERSION
from serac.validation.runout import run_suite


def _design() -> EnsembleDesign:
    return EnsembleDesign(
        n_members=8,
        seed=3,
        resolutions=((30.0, 8),),
        settings_template={"cfl": 0.45, "max_time_s": 600.0},
    )


def _metrics(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "inundation": {"threshold_m": 1.0, "median_iou": 0.81, "gate_pass": True},
        "arrival_mae_worst_s": 42.0,
        "arrival_gate_pass": True,
        "latency": {"p95_s": 0.02, "gate_pass": True, "device": "cpu"},
        "coverage": {
            "max_depth_5_95": 0.9,
            "arrival_5_95": 0.9,
            "depth_gate_pass": True,
            "arrival_gate_pass": True,
        },
        "transects": {},
        "split": {"train": ["a", "b"], "val": ["c"], "test": ["d"]},
    }
    base.update(overrides)
    return base


def _summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "n_valid": 210,
        "n_members_recorded": 230,
        "n_flagged_but_retained": 180,
        "bytes_on_disk": 1000,
        "bytes_cap": 3 * 1024**3,
        "bytes_within_cap": True,
        "frozen_solver_version": SOLVER_VERSION,
    }
    base.update(overrides)
    return base


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository laid out well enough for the suite to pass, so tests can break one thing."""
    reports = tmp_path / "reports" / "runout"
    reports.mkdir(parents=True)
    write_frozen(_design(), reports, "sized against the measured per-member cost")
    (reports / "surrogate_metrics.json").write_text(json.dumps(_metrics()), encoding="utf-8")
    (reports / "ensemble_summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (reports / "ensemble_index.jsonl").write_text("", encoding="utf-8")
    (reports / "langtang_sanity.md").write_text(
        "# Langtang\n\nNOT r.avaflow. This is a comparison, not an adjustment. "
        "Frozen design hash `abc`.\n",
        encoding="utf-8",
    )
    (tmp_path / "reports" / "MODEL_CARD_runout.md").write_text(
        f"NOT r.avaflow; cross-validation outstanding. serac-swe-voellmy v{SOLVER_VERSION}.\n",
        encoding="utf-8",
    )
    return tmp_path


def _check(result: Any, name: str) -> Any:
    return next(c for c in result.checks if c.name == name)


def test_a_well_formed_repository_passes(repo: Path) -> None:
    result = run_suite(repo, reports_dir=repo / "reports" / "runout")
    assert result.passed, [c.name for c in result.checks if c.failed]


def test_a_changed_design_hash_fails_the_gate(repo: Path) -> None:
    """The whole point of freezing: the recomputed hash must match what is written down."""
    reports = repo / "reports" / "runout"
    payload = json.loads((reports / "ensemble_design.json").read_text(encoding="utf-8"))
    payload["dimensions"][0]["high"] = 9.9e8  # a bound moved after the freeze
    (reports / "ensemble_design.json").write_text(json.dumps(payload), encoding="utf-8")

    result = run_suite(repo, reports_dir=reports)

    assert not result.passed
    assert _check(result, "frozen_design_hash_matches").failed


def test_a_changed_solver_version_fails_the_gate(repo: Path) -> None:
    reports = repo / "reports" / "runout"
    payload = json.loads((reports / "ensemble_design.json").read_text(encoding="utf-8"))
    payload["solver_version"] = "9.9.9"
    (reports / "ensemble_design.json").write_text(json.dumps(payload), encoding="utf-8")

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, "frozen_solver_version_matches").failed


@pytest.mark.parametrize("word", ["calibrated", "tuning", "best-fit", "fitted to"])
def test_calibration_language_in_a_report_fails_the_gate(repo: Path, word: str) -> None:
    """The Langtang comparison must not be describable as calibration."""
    reports = repo / "reports" / "runout"
    path = reports / "langtang_sanity.md"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\nThe member was {word} the timings.\n",
        encoding="utf-8",
    )

    result = run_suite(repo, reports_dir=reports)

    assert not result.passed
    assert _check(result, "no_calibration_language").failed


def test_a_report_without_the_disclaimer_fails_the_gate(repo: Path) -> None:
    reports = repo / "reports" / "runout"
    (reports / "extra_report.md").write_text(
        "# Something\n\nNo disclaimer here.\n", encoding="utf-8"
    )

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, "reports_disclaim_ravaflow").failed


def test_a_model_card_without_the_disclaimer_fails_the_gate(repo: Path) -> None:
    (repo / "reports" / "MODEL_CARD_runout.md").write_text(
        "Nothing to declare.\n", encoding="utf-8"
    )

    result = run_suite(repo, reports_dir=repo / "reports" / "runout")

    assert _check(result, "model_card_disclaims_ravaflow").failed


def test_overlapping_splits_fail_the_gate(repo: Path) -> None:
    """A run_id in two splits is leakage, and the suite must not take the metrics' word for it."""
    reports = repo / "reports" / "runout"
    metrics = _metrics(split={"train": ["a", "b"], "val": ["b"], "test": ["d"]})
    (reports / "surrogate_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, "splits_disjoint_by_run_id").failed


@pytest.mark.parametrize(
    ("field", "value", "check"),
    [
        ({"median_iou": 0.4, "gate_pass": False, "threshold_m": 1.0}, None, "inundation_iou_gate"),
        (None, None, "arrival_time_mae_gate"),
    ],
)
def test_a_missed_surrogate_gate_fails_the_suite(
    repo: Path, field: dict[str, Any] | None, value: Any, check: str
) -> None:
    reports = repo / "reports" / "runout"
    if field is not None:
        metrics = _metrics(inundation=field)
    else:
        metrics = _metrics(arrival_mae_worst_s=400.0, arrival_gate_pass=False)
    (reports / "surrogate_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, check).failed


def test_too_few_valid_members_fails_the_gate(repo: Path) -> None:
    reports = repo / "reports" / "runout"
    (reports / "ensemble_summary.json").write_text(
        json.dumps(_summary(n_valid=17)), encoding="utf-8"
    )

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, "ensemble_has_enough_valid_members").failed
    assert "17" in _check(result, "ensemble_has_enough_valid_members").details


def test_missing_inputs_warn_rather_than_pass_silently(tmp_path: Path) -> None:
    """A fresh clone with no ensemble must say so, not quietly report success."""
    reports = tmp_path / "reports" / "runout"
    reports.mkdir(parents=True)
    (tmp_path / "reports" / "MODEL_CARD_runout.md").write_text("NOT r.avaflow\n", encoding="utf-8")

    result = run_suite(tmp_path, reports_dir=reports)

    names = {c.name: c for c in result.checks}
    assert not names["ensemble_frozen_present"].ok
    assert not names["surrogate_metrics_present"].ok
    assert names["ensemble_frozen_present"].severity == "warning"
    assert names["langtang_sanity_present"].failed, (
        "a missing comparison is an error, not a warning"
    )


def test_a_model_card_naming_a_stale_solver_version_fails_the_gate(repo: Path) -> None:
    """The card credited v0.1.0 through the bump to v0.2.0, and the gate passed anyway.

    It only ever grepped for the disclaimer, so a card describing a different solver satisfied
    it. A model card that names the wrong version is documenting a different model.
    """
    (repo / "reports" / "MODEL_CARD_runout.md").write_text(
        "NOT r.avaflow. Simulator: serac-swe-voellmy v0.1.0.\n", encoding="utf-8"
    )

    result = run_suite(repo, reports_dir=repo / "reports" / "runout")

    assert _check(result, "model_card_names_the_current_solver_version").failed


def test_calibration_language_in_the_model_card_fails_the_gate(repo: Path) -> None:
    """The vocabulary grep covers the model card, not only `reports/runout/*.md`."""
    card = repo / "reports" / "MODEL_CARD_runout.md"
    card.write_text(
        card.read_text(encoding="utf-8") + "\nThe parameters fitted the observed arrivals.\n",
        encoding="utf-8",
    )

    result = run_suite(repo, reports_dir=repo / "reports" / "runout")

    assert _check(result, "no_calibration_language").failed


@pytest.mark.parametrize("word", ["fitted", "fit to", "matched to"])
def test_bare_fitting_vocabulary_is_caught(repo: Path, word: str) -> None:
    """Mutation check: a list holding only `fitted to` let `parameters fitted the arrivals` past."""
    path = repo / "reports" / "runout" / "langtang_sanity.md"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\nThe parameters {word} the observed arrivals.\n",
        encoding="utf-8",
    )

    result = run_suite(repo, reports_dir=repo / "reports" / "runout")

    assert _check(result, "no_calibration_language").failed


def test_a_missing_frozen_solver_version_fails_the_gate(repo: Path) -> None:
    """A *missing* version used to satisfy the version gate, because `None` was allowed."""
    reports = repo / "reports" / "runout"
    summary = _summary()
    del summary["frozen_solver_version"]
    (reports / "ensemble_summary.json").write_text(json.dumps(summary), encoding="utf-8")

    result = run_suite(repo, reports_dir=reports)

    assert _check(result, "ensemble_solver_version_matches").failed
