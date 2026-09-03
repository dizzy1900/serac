"""`validate-watch`, exercised on throwaway git repositories built inside the test.

The ancestry check is the one that matters, so it is tested against real `git` history rather
than against a mock: a repo where the pre-registration lands first must pass, and a repo where
it lands afterwards or is edited afterwards must fail.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from serac.validation.result import Suite
from serac.validation.watch import (
    BACKTEST_JSON,
    BACKTEST_MD,
    LANGTANG_MD,
    MODEL_CARD,
    PREREGISTRATION_PATH,
    check_no_failure_date_anywhere,
    run_suite,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


BACKTEST_PAYLOAD = {
    "summary": {
        "n_steps": 3,
        "n_units_total": 5,
        "reached_watch": True,
        "lead_time_days_to_first_watch": 61.0,
        "concurrent_other_watch_units_at_first_watch": 2,
        "median_watch_units_per_step": 1.0,
        "steps_by_target_tier": {"quiet": 1, "elevated": 1, "watch": 1},
        "disclaimer": (
            "The tier is ordinal. It is not a calibrated failure probability and it is never a "
            "prediction of a failure date."
        ),
    },
    "steps": [
        {"step": "2020-11-01", "target_tier": "quiet", "target_reason": None},
        {"step": "2020-12-01", "target_tier": "elevated", "target_reason": None},
        {"step": "2021-01-01", "target_tier": "watch", "target_reason": None},
    ],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _write(root, "data/manifest.jsonl", "")
    _write(
        root, MODEL_CARD, "not a time-of-failure predictor\ndecorrelation layover brittle monsoon\n"
    )
    _write(root, LANGTANG_MD, "## We could not have seen it\n\n## There was no precursor\n")
    _write(root, BACKTEST_MD, "backtest\n")
    _write(root, "tests/unit/watch/test_no_hindsight.py", "x\n")
    _write(
        root,
        "tests/unit/watch/test_anomaly.py",
        "def test_appending_future_samples_then_truncating_leaves_scores_identical():\n    pass\n",
    )
    _write(
        root,
        "reports/watch/track_selection_x.json",
        json.dumps(
            {"aoi_id": "x", "selected_path": 56, "rule_sha256": "a" * 64, "selected_reason": "r"}
        ),
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "scaffold")
    return root


def _commit_prereg(repo: Path, text: str = "thresholds 2.0 / 3.0\n") -> None:
    _write(repo, PREREGISTRATION_PATH, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prereg")


def _commit_backtest(repo: Path) -> None:
    _write(repo, BACKTEST_JSON, json.dumps(BACKTEST_PAYLOAD, indent=1))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "backtest")


def _named(result: object, name: str) -> object:
    return next(c for c in result.checks if c.name == name)  # type: ignore[attr-defined]


def test_the_suite_passes_when_the_preregistration_lands_first(repo: Path) -> None:
    _commit_prereg(repo)
    _commit_backtest(repo)
    result = run_suite(repo)
    assert _named(result, "preregistration_precedes_backtest").ok  # type: ignore[attr-defined]
    assert _named(result, "preregistration_unmodified_after_commit").ok  # type: ignore[attr-defined]
    assert result.passed, [c.name for c in result.checks if c.failed]


def test_the_suite_fails_when_the_preregistration_lands_after_the_backtest(repo: Path) -> None:
    _commit_backtest(repo)
    _commit_prereg(repo)
    result = run_suite(repo)
    assert not _named(result, "preregistration_precedes_backtest").ok  # type: ignore[attr-defined]
    assert not result.passed


def test_the_suite_fails_when_the_preregistration_is_edited_after_the_backtest(repo: Path) -> None:
    _commit_prereg(repo)
    _commit_backtest(repo)
    _commit_prereg(repo, "thresholds 1.0 / 1.5 (edited to fit the result)\n")
    result = run_suite(repo)
    assert not _named(result, "preregistration_unmodified_after_commit").ok  # type: ignore[attr-defined]
    assert not result.passed


def test_the_suite_fails_when_the_backtest_and_preregistration_share_a_commit(repo: Path) -> None:
    """Landing both together proves nothing about which came first."""
    _write(repo, PREREGISTRATION_PATH, "thresholds\n")
    _write(repo, BACKTEST_JSON, json.dumps(BACKTEST_PAYLOAD))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "both at once")
    result = run_suite(repo)
    assert not _named(result, "preregistration_precedes_backtest").ok  # type: ignore[attr-defined]


def test_the_suite_fails_without_a_langtang_writeup(repo: Path) -> None:
    (repo / LANGTANG_MD).unlink()
    _commit_prereg(repo)
    _commit_backtest(repo)
    result = run_suite(repo)
    assert not _named(result, "langtang_result_written").ok  # type: ignore[attr-defined]


def test_the_suite_fails_when_langtang_does_not_separate_the_two_negatives(repo: Path) -> None:
    _write(repo, LANGTANG_MD, "Nothing was found.\n")
    _commit_prereg(repo)
    _commit_backtest(repo)
    result = run_suite(repo)
    assert not _named(  # type: ignore[attr-defined]
        result, "langtang_separates_observability_from_absence_of_precursor"
    ).ok


def test_the_suite_fails_when_a_step_reports_a_reason_but_a_measurable_tier(repo: Path) -> None:
    payload = json.loads(json.dumps(BACKTEST_PAYLOAD))
    payload["steps"][0]["target_reason"] = "low_coherence"
    payload["steps"][0]["target_tier"] = "quiet"
    _commit_prereg(repo)
    _write(repo, BACKTEST_JSON, json.dumps(payload))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "backtest")
    result = run_suite(repo)
    assert not _named(result, "insufficient_data_honoured").ok  # type: ignore[attr-defined]


def test_the_suite_fails_without_the_model_card_disclaimer(repo: Path) -> None:
    _write(repo, MODEL_CARD, "a model card with no disclaimer\n")
    _commit_prereg(repo)
    _commit_backtest(repo)
    result = run_suite(repo)
    assert not _named(result, "model_card_disclaimer_present").ok  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "offender",
    ["failure_date", "days_to_failure", "failure_probability", "predicted_failure_window"],
)
def test_a_predicted_date_or_probability_field_fails_the_gate(
    tmp_path: Path, offender: str
) -> None:
    (tmp_path / "reports" / "watch").mkdir(parents=True)
    (tmp_path / "reports" / "watch" / "x.json").write_text(
        json.dumps({offender: "2021-02-07"}), encoding="utf-8"
    )
    suite = Suite("watch", tmp_path)
    check_no_failure_date_anywhere(suite, tmp_path)
    assert not suite.checks[0].ok
    assert offender in suite.checks[0].details


def test_recording_the_observed_time_of_a_past_event_is_allowed(tmp_path: Path) -> None:
    """`failure_time_utc` is a fact about history, not a prediction, and must not trip the gate."""
    (tmp_path / "reports" / "watch").mkdir(parents=True)
    (tmp_path / "reports" / "watch" / "x.json").write_text(
        json.dumps({"failure_time_utc": "2021-02-07T04:51:00Z"}), encoding="utf-8"
    )
    suite = Suite("watch", tmp_path)
    check_no_failure_date_anywhere(suite, tmp_path)
    assert suite.checks[0].ok
