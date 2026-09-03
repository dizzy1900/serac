"""The latency floor is stated, the budget verdict is honest, and the gate runs offline."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import REPO_ROOT

from serac.models.discriminator import latency as lat
from serac.validation.discriminator import run_suite


def _mode(mode: str, stream_latency: float | None) -> lat.ModeLatency:
    return lat.ModeLatency(
        mode=mode,
        fired=stream_latency is not None,
        stream_latency_s=stream_latency,
        compute_seconds_total=1.0,
        compute_seconds_per_poll_p50=0.01,
        compute_seconds_per_poll_p95=0.02,
        polls=10,
        windows_scored=1,
        chunks_ingested=100,
        theoretical_floor_s=lat.theoretical_floor_s(mode),  # type: ignore[arg-type]
        meets_brief_budget=bool(stream_latency is not None and stream_latency <= 60.0),
    )


def test_the_batch_floor_exceeds_the_brief_budget_by_construction() -> None:
    """Travel time to a >=100 km receiver plus a 600 s window cannot fit in 60 s."""
    floor = lat.theoretical_floor_s("batch_600s")
    assert floor > lat.BRIEF_BUDGET_S
    assert floor == pytest.approx(100.0 / 3.0 + 600.0 - 60.0)


def test_the_sliding_floor_also_exceeds_the_budget() -> None:
    assert lat.theoretical_floor_s("sliding_180s") > lat.BRIEF_BUDGET_S


def test_the_verdict_says_plainly_that_the_budget_is_unreachable() -> None:
    report = lat.build_report(
        "langtang-lhende-2026",
        datetime(2026, 8, 26, 2, 52, 10, tzinfo=UTC),
        [_mode("batch_600s", 545.0), _mode("sliding_180s", 190.0)],
        n_receivers=8,
    )
    assert report.budget_met is False
    assert "NOT met" in report.verdict
    assert "not reachable" in report.verdict


def test_a_run_that_never_fired_is_not_reported_as_a_budget_pass() -> None:
    report = lat.build_report(
        "x",
        datetime(2026, 1, 1, tzinfo=UTC),
        [_mode("batch_600s", None)],
        n_receivers=3,
    )
    assert report.budget_met is False
    assert "not a budget pass" in report.verdict


def test_the_gate_runs_offline_and_always_checks_the_feature_names() -> None:
    result = run_suite(REPO_ROOT)
    passed = {c.name for c in result.checks if c.ok}
    assert "no_forbidden_feature_tokens" in passed


def test_the_gate_refuses_to_go_green_on_a_tree_with_no_window_index(tmp_path) -> None:
    """This test used to assert the opposite, and it was wrong.

    An empty tree proves nothing about leakage, so reporting `passed` there was the suite
    telling a comfortable lie. The window index is committed now, so the real fresh-clone
    case does run the assertions; a tree without even the index must fail.
    """
    result = run_suite(tmp_path)
    assert not result.passed
    failed = {c.name for c in result.checks if c.failed}
    assert "leakage_criteria_provable" in failed
