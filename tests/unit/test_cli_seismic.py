"""`serac ingest fdsn|comcat|hydro` through Typer's CliRunner (offline; dry runs only)."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from serac.cli_seismic import app

runner = CliRunner()


def test_comcat_dry_run_prints_plan_and_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "comcat",
            "--start",
            "2026-08-25",
            "--end",
            "2026-08-27",
            "--dry-run",
            "--repo",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "eventtype=landslide" in result.output
    assert "estimated_bytes: None" in result.output
    assert "dry run: nothing written" in result.output
    assert not (tmp_path / "data").exists()


def test_hydro_shows_reported_figures(repo_root: Path) -> None:
    result = runner.invoke(app, ["hydro", "--repo", str(repo_root)])
    assert result.exit_code == 0, result.output
    assert "status=fetched" in result.output
    assert "no open real-time Nepal/China hydrometric feed" in result.output
    assert "station galchhi" in result.output
    assert "stage_change_m=9.0 over 1800" in result.output
    assert "stage_change_m=7.0 over 1800" in result.output


def test_hydro_dry_run_only_reports_status(repo_root: Path) -> None:
    result = runner.invoke(app, ["hydro", "--dry-run", "--repo", str(repo_root)])
    assert result.exit_code == 0, result.output
    assert "station galchhi" not in result.output


def test_hydro_not_fetched_fixture_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "data" / "fixtures" / "hydro" / "icimod_trishuli_2026-08-26.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status": "not_fetched", "reason": "page unreachable in session"}')
    result = runner.invoke(app, ["hydro", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "not fetched" in result.output


def test_fdsn_requires_sncl_or_coordinates(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fdsn",
            "--event",
            "x",
            "--start",
            "2021-02-07T04:49:00",
            "--end",
            "2021-02-07T04:57:00",
            "--dry-run",
            "--repo",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    # Match on bare tokens: rich colours and may wrap the message at a hyphen, so asserting
    # on the literal "--sncl" is a test of the renderer, not of the error we raise.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "sncl" in plain and "lat" in plain and "lon" in plain
