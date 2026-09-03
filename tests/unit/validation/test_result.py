from __future__ import annotations

from pathlib import Path

from serac.validation.result import Severity, Suite, load_report, write_report


def test_suite_collects_checks_and_status(tmp_path: Path) -> None:
    s = Suite("demo", repo=tmp_path)
    assert s.check("a", True)
    assert not s.warn("b", False, "soft")
    s.info("c", "note")
    r = s.result()
    assert r.passed and r.status == "passed"
    assert r.git_sha is None  # tmp_path is not a git checkout
    assert "1 warnings" in r.summary()
    s.check("d", False, "hard")
    assert not s.result().passed


def test_write_and_load_report(tmp_path: Path) -> None:
    s = Suite("demo")
    s.check("x", False, severity=Severity.error)
    path = write_report(s.result(), tmp_path / "reports")
    loaded = load_report(path)
    assert loaded.suite == "demo" and loaded.status == "failed"
    assert path.name == "demo.json"
