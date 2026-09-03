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
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from serac import __version__

VALIDATION_CONTRACT_VERSION = "0.1.0"


class Severity(StrEnum):
    error = "error"
    warning = "warning"
    info = "info"


class Check(BaseModel):
    """One named check with its outcome and human-readable evidence."""

    name: str = Field(min_length=1)
    ok: bool
    severity: Severity = Severity.error
    details: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok and self.severity == Severity.error


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

    def summary(self) -> str:
        n_err = sum(1 for c in self.checks if c.failed)
        n_warn = sum(1 for c in self.checks if not c.ok and c.severity == Severity.warning)
        return (
            f"{self.suite}: {self.status} "
            f"({len(self.checks)} checks, {n_err} errors, {n_warn} warnings)"
        )


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
        mark = "ok  " if c.ok else ("FAIL" if c.severity == Severity.error else "warn")
        line = f"  {mark} {c.name}"
        if c.details:
            line += f" — {c.details}"
        print(line)  # noqa: T201
    print(result.summary())  # noqa: T201


def load_report(path: Path) -> SuiteResult:
    return SuiteResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
