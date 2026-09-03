from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from serac.cli import app
from serac.validation.promote import (
    PromotionRefusedError,
    make_stamp,
    promote,
    promotion_blockers,
    write_stamp,
)
from serac.validation.result import Check, SuiteResult, write_report

SUITES = ("events", "aoi", "ingest", "cube", "stream", "contracts")


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
    record = promote(repo, reports, repo / "reports" / "promotion")
    assert (repo / "reports" / "promotion" / f"{record.git_sha}.json").exists()


def test_promote_refuses_without_stamp(repo: Path) -> None:
    assert promotion_blockers(repo, None) == [
        "no validation stamp: run `make validate-serac` first"
    ]


def test_promote_refuses_failed_or_missing_suite(repo: Path) -> None:
    reports = _reports(repo, failing="cube")
    stamp = make_stamp(repo, reports)
    assert not stamp.passed
    write_stamp(stamp, reports)
    with pytest.raises(PromotionRefusedError, match="suites failed: cube"):
        promote(repo, reports, repo / "reports" / "promotion")
    reports = _reports(repo, skip="aoi")
    stamp = make_stamp(repo, reports)
    assert stamp.missing == ["aoi"]


def test_promote_refuses_dirty_tree_and_stale_sha(repo: Path) -> None:
    reports = _reports(repo)
    write_stamp(make_stamp(repo, reports), reports)
    (repo / "dirty.txt").write_text("y")
    blockers = promotion_blockers(repo, make_stamp(repo, reports))
    assert any("not clean" in b for b in blockers)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    stamp = make_stamp(repo, reports)
    stamp = stamp.model_copy(update={"git_sha": "0" * 40, "tree_clean": True})
    blockers = promotion_blockers(repo, stamp)
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
    )
    assert res.exit_code == 0 and "promotable:" in res.output


def test_cli_unimplemented_suite_fails_loudly(tmp_path: Path) -> None:
    res = CliRunner().invoke(
        app, ["validate", "events", "--repo", str(tmp_path), "--reports-dir", str(tmp_path)]
    )
    assert res.exit_code != 0
