"""Shared result types for the validation harness (`make validate-*`).

Each suite module exposes `run_suite(...) -> SuiteResult`. The orchestrating CLI writes the
result to `reports/validation/<suite>.json` and exits non-zero when any `error` check fails.
Warnings never fail a suite; they exist so honest gaps (e.g. a fixture recorded as
`not_fetched`) are visible without being silently tolerated.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, Field

from serac import __version__

VALIDATION_CONTRACT_VERSION = "0.1.0"

EXIT_ERROR = 1
EXIT_CRITERION_UNMET = 3


class Severity(StrEnum):
    """How a failing check should be read.

    `error` means something is broken or inconsistent. `criterion_unmet` means the code is
    working correctly and reporting that a criterion the brief sets has not been met -- a
    different fact, and one a reader must not mistake for a bug. Both fail a suite: a gate
    that goes green while its own criterion is unmet is worse than one that fails loudly.
    """

    error = "error"
    criterion_unmet = "criterion_unmet"
    warning = "warning"
    info = "info"


class Check(BaseModel):
    """One named check with its outcome and human-readable evidence."""

    name: str = Field(min_length=1)
    ok: bool
    severity: Severity = Severity.error
    details: str = ""

    FAILING_SEVERITIES: ClassVar[frozenset[Severity]] = frozenset(
        {Severity.error, Severity.criterion_unmet}
    )

    @property
    def failed(self) -> bool:
        return not self.ok and self.severity in self.FAILING_SEVERITIES


class SuiteResult(BaseModel):
    """The outcome of one validation suite."""

    contract_version: str = VALIDATION_CONTRACT_VERSION
    suite: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    serac_version: str = __version__
    git_sha: str | None = None
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return not any(c.failed for c in self.checks)

    @property
    def status(self) -> Literal["passed", "failed"]:
        return "passed" if self.passed else "failed"

    @property
    def unmet_criteria(self) -> list[str]:
        """Names of checks reporting an unmet criterion rather than a defect."""
        return [c.name for c in self.checks if not c.ok and c.severity == Severity.criterion_unmet]

    @property
    def exit_code(self) -> int:
        """0 passed, 1 something is broken, 3 the code worked and a criterion was not met.

        The distinction is the whole point of `Severity.criterion_unmet`, and a caller that
        collapses it back to "non-zero" cannot tell a regression from an honest negative
        result. `EXIT_CRITERION_UNMET` is 3 rather than 2 because `underwriting-check`
        already spends 2 on a different meaning.
        """
        if self.passed:
            return 0
        has_error = any(c.failed and c.severity != Severity.criterion_unmet for c in self.checks)
        return EXIT_ERROR if has_error else EXIT_CRITERION_UNMET

    def summary(self) -> str:
        n_unmet = len(self.unmet_criteria)
        n_err = sum(1 for c in self.checks if c.failed) - n_unmet
        n_warn = sum(1 for c in self.checks if not c.ok and c.severity == Severity.warning)
        parts = [f"{len(self.checks)} checks", f"{n_err} errors"]
        if n_unmet:
            # Named separately: an unmet criterion means the code worked and the target was
            # not reached, which a reader must not confuse with a defect.
            parts.append(f"{n_unmet} unmet criteria")
        parts.append(f"{n_warn} warnings")
        return f"{self.suite}: {self.status} ({', '.join(parts)})"


def git_sha(repo: Path | None = None) -> str | None:
    """Current HEAD sha, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


class Suite:
    """Collects checks while a suite runs, then freezes them into a `SuiteResult`."""

    def __init__(self, name: str, repo: Path | None = None) -> None:
        self.name = name
        self.repo = repo
        self.started_at = datetime.now(tz=UTC)
        self.checks: list[Check] = []

    def check(
        self, name: str, ok: bool, details: str = "", severity: Severity = Severity.error
    ) -> bool:
        self.checks.append(Check(name=name, ok=ok, severity=severity, details=details))
        return ok

    def criterion(self, name: str, ok: bool, details: str = "") -> bool:
        """Record a criterion the brief sets. Fails the suite when unmet, but says why."""
        return self.check(name, ok, details, Severity.criterion_unmet)

    def warn(self, name: str, ok: bool, details: str = "") -> bool:
        return self.check(name, ok, details, Severity.warning)

    def info(self, name: str, details: str) -> None:
        self.check(name, True, details, Severity.info)

    def result(self) -> SuiteResult:
        return SuiteResult(
            suite=self.name,
            started_at=self.started_at,
            finished_at=datetime.now(tz=UTC),
            git_sha=git_sha(self.repo),
            checks=list(self.checks),
        )


def write_report(result: SuiteResult, reports_dir: Path) -> Path:
    """Write `<reports_dir>/<suite>.json` and return its path."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{result.suite}.json"
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def print_result(result: SuiteResult) -> None:
    """Human-readable dump of a suite result."""
    for c in result.checks:
        if c.ok:
            mark = "ok  "
        elif c.severity == Severity.error:
            mark = "FAIL"
        elif c.severity == Severity.criterion_unmet:
            mark = "UNMET"
        else:
            mark = "warn"
        line = f"  {mark} {c.name}"
        if c.details:
            line += f" — {c.details}"
        print(line)  # noqa: T201
    print(result.summary())  # noqa: T201


def load_report(path: Path) -> SuiteResult:
    return SuiteResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
