from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from serac.cli import app
from serac.validation.promote import (
    APPROVAL_ENV_VAR,
    REQUIRED_SUITES,
    PromotionRecord,
    PromotionRefusedError,
    approval_blocker,
    approver_name,
    make_stamp,
    promote,
    promotion_blockers,
    write_stamp,
)
from serac.validation.result import Check, SuiteResult, write_report

# Track the real list, so adding a gate cannot leave these fixtures silently stale.
SUITES = REQUIRED_SUITES

APPROVER = "R. Approver"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("reports/\n")
    (tmp_path / "f.txt").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _reports(repo: Path, failing: str | None = None, skip: str | None = None) -> Path:
    reports = repo / "reports" / "validation"
    now = datetime.now(tz=UTC)
    for name in SUITES:
        if name == skip:
            (reports / f"{name}.json").unlink(missing_ok=True)
            continue
        ok = name != failing
        write_report(
            SuiteResult(
                suite=name, started_at=now, finished_at=now, checks=[Check(name="c", ok=ok)]
            ),
            reports,
        )
    return reports


def test_stamp_and_promote_happy_path(repo: Path) -> None:
    reports = _reports(repo)
    stamp = make_stamp(repo, reports)
    assert stamp.passed and stamp.tree_clean and stamp.git_sha == _git(repo, "rev-parse", "HEAD")
    write_stamp(stamp, reports)
    record = promote(repo, reports, repo / "reports" / "promotion", APPROVER)
    written = repo / "reports" / "promotion" / f"{record.git_sha}.json"
    assert written.exists()
    assert record.approved_by == APPROVER
    # The approver is in the record on disk, not only in the returned object.
    assert json.loads(written.read_text())["approved_by"] == APPROVER


def test_promote_refuses_without_stamp(repo: Path) -> None:
    blockers = promotion_blockers(repo, None, APPROVER)
    assert blockers == ["no validation stamp: run `make validate-serac` first"]
    # Every blocker is collected: a missing stamp must not hide a missing approver.
    both = promotion_blockers(repo, None, None)
    assert len(both) == 2 and any(APPROVAL_ENV_VAR in b for b in both)


def test_promote_refuses_failed_or_missing_suite(repo: Path) -> None:
    reports = _reports(repo, failing="cube")
    stamp = make_stamp(repo, reports)
    assert not stamp.passed
    write_stamp(stamp, reports)
    with pytest.raises(PromotionRefusedError, match="suites failed: cube"):
        promote(repo, reports, repo / "reports" / "promotion", APPROVER)
    reports = _reports(repo, skip=SUITES[1])
    stamp = make_stamp(repo, reports)
    assert stamp.missing == [SUITES[1]]


def test_promote_refuses_dirty_tree_and_stale_sha(repo: Path) -> None:
    reports = _reports(repo)
    write_stamp(make_stamp(repo, reports), reports)
    (repo / "dirty.txt").write_text("y")
    blockers = promotion_blockers(repo, make_stamp(repo, reports), APPROVER)
    assert any("not clean" in b for b in blockers)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    stamp = make_stamp(repo, reports)
    stamp = stamp.model_copy(update={"git_sha": "0" * 40, "tree_clean": True})
    blockers = promotion_blockers(repo, stamp, APPROVER)
    assert any("HEAD is" in b for b in blockers)


def test_cli_stamp_and_promote(repo: Path) -> None:
    reports = _reports(repo)
    runner = CliRunner()
    res = runner.invoke(
        app, ["validate", "stamp", "--repo", str(repo), "--reports-dir", str(reports)]
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(
        app,
        [
            "promote",
            "--repo",
            str(repo),
            "--reports-dir",
            str(reports),
            "--promotions-dir",
            str(repo / "reports/promotion"),
        ],
        env={APPROVAL_ENV_VAR: APPROVER},
    )
    assert res.exit_code == 0 and "promotable:" in res.output
    assert f"approved_by={APPROVER}" in res.output


def test_cli_unimplemented_suite_fails_loudly(tmp_path: Path) -> None:
    res = CliRunner().invoke(
        app, ["validate", "events", "--repo", str(tmp_path), "--reports-dir", str(tmp_path)]
    )
    assert res.exit_code != 0


def test_promote_refuses_without_a_human_approver(repo: Path) -> None:
    """The gate the brief asks for: no named human, no promotion."""
    reports = _reports(repo)
    write_stamp(make_stamp(repo, reports), reports)
    promotions = repo / "reports" / "promotion"
    for value in (None, "", "   ", "\t\n"):
        with pytest.raises(PromotionRefusedError, match=APPROVAL_ENV_VAR):
            promote(repo, reports, promotions, value)
    assert not promotions.exists(), "a refused promotion must write no record"


@pytest.mark.parametrize("value", ["1", "yes", "TRUE", " ci ", "approved", "bot", "n/a", "x"])
def test_promote_refuses_an_approver_that_names_no_one(repo: Path, value: str) -> None:
    """A boolean-ish or job-shaped value would let a script rubber-stamp itself."""
    reports = _reports(repo)
    write_stamp(make_stamp(repo, reports), reports)
    assert approver_name(value) is None
    with pytest.raises(PromotionRefusedError, match="names no one"):
        promote(repo, reports, repo / "reports" / "promotion", value)


@pytest.mark.parametrize("value", ["R. Approver", "  D. Sharma  ", "ci-lead@example.invalid"])
def test_approver_name_accepts_and_strips_a_real_name(value: str) -> None:
    name = approver_name(value)
    assert name == value.strip() and approval_blocker(value) is None


def test_promotion_record_cannot_be_built_without_an_approver() -> None:
    """The structural half of the gate: an unapproved record is unrepresentable."""
    fields = {
        "promoted_at": datetime.now(tz=UTC),
        "git_sha": "0" * 40,
        "stamp_path": "reports/validation/latest.json",
        "suites": {name: "passed" for name in SUITES},
    }
    with pytest.raises(ValidationError):
        PromotionRecord(**fields)
    with pytest.raises(ValidationError):
        PromotionRecord(**fields, approved_by="")


def test_cli_promote_refuses_when_the_env_var_is_unset(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make promote` passes no flag; the env var is the whole channel."""
    reports = _reports(repo)
    monkeypatch.delenv(APPROVAL_ENV_VAR, raising=False)
    res = CliRunner().invoke(
        app, ["validate", "stamp", "--repo", str(repo), "--reports-dir", str(reports)]
    )
    assert res.exit_code == 0, res.output
    res = CliRunner().invoke(
        app,
        [
            "promote",
            "--repo",
            str(repo),
            "--reports-dir",
            str(reports),
            "--promotions-dir",
            str(repo / "reports/promotion"),
        ],
    )
    assert res.exit_code == 1
    assert APPROVAL_ENV_VAR in res.output
    assert not (repo / "reports" / "promotion").exists()


def test_makefile_promote_requires_the_env_var() -> None:
    """The Makefile must not quietly supply an approver of its own."""
    makefile = Path(__file__).resolve().parents[3] / "Makefile"
    lines = makefile.read_text(encoding="utf-8").splitlines()
    promote_rule = next(ln for ln in lines if ln.startswith("promote:"))
    assert "require-approval" in promote_rule
    body = lines[lines.index(promote_rule) + 1]
    assert body.strip().endswith("serac promote"), (
        "the Makefile must not hand `serac promote` an approver of its own"
    )
