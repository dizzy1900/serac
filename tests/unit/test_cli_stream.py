"""`serac stream ...` / `serac replay` through Typer's CliRunner (offline)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from serac.cli_stream import app

runner = CliRunner()


def test_replay_chamoli_at_speed_max_writes_report(repo_root: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "replay",
            "--event",
            "chamoli-2021",
            "--speed",
            "max",
            "--report-dir",
            str(tmp_path),
            "--repo",
            str(repo_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "chamoli-2021: completed" in result.output
    assert "stub=True" in result.output
    report = json.loads((tmp_path / "chamoli-2021.json").read_text())
    assert report["is_stub"] is True
    assert report["counts"]["pending_after_drain"] == 0
    assert report["wall_clock_latencies"]["valid"] is False


def test_replay_synthetic_lane(repo_root: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "replay",
            "--event",
            "synthetic-lp-burst",
            "--report-dir",
            str(tmp_path),
            "--repo",
            str(repo_root),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "synthetic-lp-burst.json").read_text())
    assert report["contains_synthetic"] is True
    assert report["counts"]["detections_emitted"] >= 1
    assert report["counts"]["cap_messages_emitted"] >= 1


def test_replay_missing_fixture_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["replay", "--event", "nope", "--report-dir", str(tmp_path), "--repo", str(repo_root)]
    )
    assert result.exit_code == 2
    assert "not fetched" in result.output


def test_replay_rejects_bad_speed(repo_root: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["replay", "--event", "synthetic-lp-burst", "--speed", "warp", "--repo", str(repo_root)],
    )
    assert result.exit_code != 0


def test_golden_check_matches_committed_file(repo_root: Path) -> None:
    result = runner.invoke(app, ["golden", "--repo", str(repo_root)])
    assert result.exit_code == 0, result.output
    assert "golden matches" in result.output


def test_golden_update_writes_file(repo_root: Path, tmp_path: Path) -> None:
    # A repo copy that lacks the golden file: --update creates it, a plain check then passes.
    root = tmp_path / "repo"
    src = repo_root / "data" / "fixtures" / "seismic" / "chamoli-2021"
    dst = root / "data" / "fixtures" / "seismic" / "chamoli-2021"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        (dst / f.name).write_bytes(f.read_bytes())
    missing = runner.invoke(app, ["golden", "--repo", str(root)])
    assert missing.exit_code == 1
    written = runner.invoke(app, ["golden", "--update", "--repo", str(root)])
    assert written.exit_code == 0, written.output
    assert (root / "tests" / "fixtures" / "golden" / "detector_stub_chamoli-2021.json").exists()
    assert runner.invoke(app, ["golden", "--repo", str(root)]).exit_code == 0


def test_run_seedlink_dry_run_does_not_connect() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "seedlink",
            "--stream",
            "NK.KKN..BHZ",
            "--server",
            "example.invalid:18000",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "example.invalid:18000" in result.output
    assert "verified_live: False" in result.output


def test_run_detector_on_in_memory_bus_idles_until_deadline() -> None:
    result = runner.invoke(app, ["run", "detector", "--bus", "in_memory", "--max-seconds", "0"])
    assert result.exit_code == 0, result.output
    assert "detector-stub: processed 0" in result.output


def test_run_cap_on_in_memory_bus(repo_root: Path) -> None:
    result = runner.invoke(
        app, ["run", "cap", "--bus", "in_memory", "--max-seconds", "0", "--repo", str(repo_root)]
    )
    assert result.exit_code == 0, result.output
    assert "cap-stub: processed 0" in result.output


def test_unknown_bus_is_rejected() -> None:
    result = runner.invoke(
        app, ["run", "detector", "--bus", "carrier-pigeon", "--max-seconds", "0"]
    )
    assert result.exit_code != 0


def test_the_live_detector_stage_defaults_to_the_stub() -> None:
    """The default stays the stub while validate-discriminator reports an unmet criterion."""
    result = runner.invoke(app, ["run", "detector", "--max-seconds", "0.1"])
    assert result.exit_code == 0
    assert "detector-stub" in result.output or "stub" in result.output
    assert "discriminator-lgbm" not in result.output


def test_the_live_detector_stage_can_mount_the_trained_model() -> None:
    """Gap 56: the replay lane could select the trained detector and the live lane could not."""
    result = runner.invoke(
        app,
        ["run", "detector", "--detector", "discriminator", "--max-seconds", "0.1"],
    )
    assert result.exit_code == 0, result.output
    assert "is_stub=False" in result.output
    assert "discriminator-lgbm" in result.output
    # Mounting a real detector does not make the lane an alert system, and the command says so.
    assert "status=Test" in result.output


def test_an_unknown_detector_is_refused_rather_than_defaulted() -> None:
    result = runner.invoke(app, ["run", "detector", "--detector", "magic", "--max-seconds", "0.1"])
    assert result.exit_code == 1
    assert "unknown detector" in result.output


def test_a_missing_artifact_is_an_error_not_a_silent_fallback(tmp_path: Path) -> None:
    """A lane that says it ran the trained model must have run it."""
    result = runner.invoke(
        app,
        [
            "run",
            "detector",
            "--detector",
            "discriminator",
            "--repo",
            str(tmp_path),
            "--max-seconds",
            "0.1",
        ],
    )
    assert result.exit_code == 1
    assert "no trained discriminator" in result.output
    assert "stub" not in result.output.split("no trained discriminator")[0]
