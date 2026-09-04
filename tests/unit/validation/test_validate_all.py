"""`serac validate all` and the exit code that distinguishes a defect from an unmet criterion.

The aggregate exists because make stops at the first failing prerequisite: with the suites
wired as prerequisites, one suite reporting an unmet criterion hid the three listed after it,
so a reader learned nothing about M3, M4 or M5. These tests pin the behaviour that replaced
it, including the part that matters most -- that every suite runs even when an earlier one
fails.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from serac.cli import app
from serac.validation.result import (
    EXIT_CRITERION_UNMET,
    EXIT_ERROR,
    Check,
    Severity,
    SuiteResult,
)

runner = CliRunner()


def _result(suite: str, *checks: Check) -> SuiteResult:
    now = datetime.now(UTC)
    return SuiteResult(suite=suite, started_at=now, finished_at=now, checks=list(checks))


def _ok(name: str = "fine") -> Check:
    return Check(name=name, ok=True, details="")


def _error(name: str = "broken") -> Check:
    return Check(name=name, ok=False, severity=Severity.error, details="")


def _unmet(name: str = "target_not_reached") -> Check:
    return Check(name=name, ok=False, severity=Severity.criterion_unmet, details="")


class TestExitCode:
    def test_passing_suite_exits_zero(self) -> None:
        assert _result("s", _ok()).exit_code == 0

    def test_an_error_exits_one(self) -> None:
        assert _result("s", _ok(), _error()).exit_code == EXIT_ERROR

    def test_an_unmet_criterion_exits_three(self) -> None:
        assert _result("s", _ok(), _unmet()).exit_code == EXIT_CRITERION_UNMET

    def test_an_error_outranks_an_unmet_criterion(self) -> None:
        # Both fail the suite, but a reader must be told that something is broken.
        assert _result("s", _unmet(), _error()).exit_code == EXIT_ERROR

    def test_a_warning_alone_does_not_fail(self) -> None:
        warned = Check(name="w", ok=False, severity=Severity.warning, details="")
        assert _result("s", _ok(), warned).exit_code == 0


@dataclass
class FakeSuites:
    """Canned results by suite name, plus the order the aggregate actually ran them in."""

    canned: dict[str, SuiteResult] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)

    def __setitem__(self, name: str, result: SuiteResult) -> None:
        self.canned[name] = result


@pytest.fixture
def fake_suites(monkeypatch: pytest.MonkeyPatch) -> FakeSuites:
    """Replace every real suite with a canned result, so the aggregate's own logic is tested."""
    from serac import cli_validate

    suites = FakeSuites()

    def _loader(name: str) -> Callable[..., SuiteResult]:
        def _run(repo: Path, **_: object) -> SuiteResult:
            suites.ran.append(name)
            return suites.canned.get(name, _result(name, _ok()))

        return _run

    monkeypatch.setattr(cli_validate, "_load_runner", _loader)
    return suites


def _invoke(tmp_path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["validate", "all", "--repo", str(tmp_path), "--reports-dir", str(tmp_path / "r"), *args],
    )
    return result.exit_code, result.output


class TestRunAll:
    def test_a_failing_suite_does_not_hide_the_suites_after_it(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        from serac.validation.promote import REQUIRED_SUITES

        first, last = REQUIRED_SUITES[0], REQUIRED_SUITES[-1]
        fake_suites[first] = _result(first, _error())
        code, output = _invoke(tmp_path)
        assert code == EXIT_ERROR
        # The regression this replaced: everything after the failure never ran.
        assert fake_suites.ran == list(REQUIRED_SUITES)
        assert f"{last}: passed" in output

    def test_unmet_only_exits_three_and_writes_no_stamp(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        from serac.validation.promote import REQUIRED_SUITES

        name = REQUIRED_SUITES[1]
        fake_suites[name] = _result(name, _unmet("forced_groups_detected"))
        code, output = _invoke(tmp_path)
        assert code == EXIT_CRITERION_UNMET
        assert "an unmet criterion is not a pass" in output
        assert not (tmp_path / "r" / "stamp.json").exists()

    def test_allow_unmet_accepts_exactly_the_named_criteria(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        from serac.validation.promote import REQUIRED_SUITES

        name = REQUIRED_SUITES[1]
        fake_suites[name] = _result(name, _unmet("known_gap"))
        code, output = _invoke(tmp_path, "--allow-unmet", "known_gap")
        assert code == 0
        assert "already reports" in output

    def test_allow_unmet_does_not_excuse_a_different_criterion(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        from serac.validation.promote import REQUIRED_SUITES

        name = REQUIRED_SUITES[1]
        fake_suites[name] = _result(name, _unmet("something_new"))
        code, output = _invoke(tmp_path, "--allow-unmet", "known_gap")
        assert code == EXIT_CRITERION_UNMET
        assert "something_new" in output

    def test_allow_unmet_never_excuses_a_broken_suite(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        from serac.validation.promote import REQUIRED_SUITES

        name = REQUIRED_SUITES[1]
        fake_suites[name] = _result(name, _error("broken"))
        code, _ = _invoke(tmp_path, "--allow-unmet", "broken")
        assert code == EXIT_ERROR

    def test_a_resolved_criterion_is_announced_not_ignored(
        self, tmp_path: Path, fake_suites: FakeSuites
    ) -> None:
        code, output = _invoke(tmp_path, "--allow-unmet", "known_gap")
        assert code == 0
        assert "no longer unmet" in output

    def test_the_discriminator_is_a_required_suite(self) -> None:
        # Excluded, a passing stamp -- and therefore `make promote` -- would be reachable
        # while M1's own criterion of the brief is unmet.
        from serac.validation.promote import REQUIRED_SUITES

        assert "discriminator" in REQUIRED_SUITES
