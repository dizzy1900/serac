"""`serac ingest` CLI: dry runs write nothing; usage errors exit non-zero."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from serac.cli_ingest import app, load_aoi_bbox, parse_bbox, parse_date

runner = CliRunner()


def test_parse_helpers() -> None:
    assert parse_bbox("79.68, 30.33,79.80,30.42") == (79.68, 30.33, 79.80, 30.42)
    with pytest.raises(Exception, match="W,S,E,N"):
        parse_bbox("1,2,3")
    with pytest.raises(Exception, match="numbers"):
        parse_bbox("a,b,c,d")
    start = parse_date("2021-02-01")
    end = parse_date("2021-02-10", end=True)
    assert start.isoformat() == "2021-02-01T00:00:00+00:00"
    assert end.isoformat() == "2021-02-10T23:59:59+00:00"
    assert parse_date("2021-02-01T05:30:00Z").hour == 5
    with pytest.raises(Exception, match="ISO"):
        parse_date("yesterday")


def test_dem_dry_run_prints_plan_and_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "dem",
            "--aoi",
            "chamoli-rishiganga",
            "--bbox",
            "79.68,30.33,79.80,30.42",
            "--buffer-m",
            "0",
            "--dry-run",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dem_glo30" in result.output and "N30_00_E079" in result.output
    assert "325 x 433 px" in result.output
    assert "nothing written" in result.output
    assert not (tmp_path / "data").exists()


def test_exactly_one_of_dry_run_or_yes(tmp_path: Path) -> None:
    common = ["dem", "--aoi", "x", "--bbox", "79.68,30.33,79.80,30.42", "--data-dir", str(tmp_path)]
    assert runner.invoke(app, common).exit_code == 2
    assert runner.invoke(app, [*common, "--dry-run", "--yes"]).exit_code == 2
    assert not (tmp_path / "manifest.jsonl").exists()


def test_bbox_from_aoi_json(tmp_path: Path) -> None:
    assert load_aoi_bbox("nope", tmp_path) is None
    aoi_dir = tmp_path / "aoi" / "lhende-khola-trishuli"
    aoi_dir.mkdir(parents=True)
    (aoi_dir / "aoi.json").write_text(json.dumps({"bbox_4326": [85.51, 28.27, 85.53, 28.29]}))
    assert load_aoi_bbox("lhende-khola-trishuli", tmp_path) == (85.51, 28.27, 85.53, 28.29)
    result = runner.invoke(
        app, ["dem", "--aoi", "lhende-khola-trishuli", "--dry-run", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "N28_00_E085" in result.output
    missing = runner.invoke(
        app, ["dem", "--aoi", "other", "--dry-run", "--data-dir", str(tmp_path)]
    )
    assert missing.exit_code == 2


def test_s2_requires_dates(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["s2-earthsearch", "--aoi", "x", "--bbox", "79.68,30.33,79.80,30.42", "--dry-run"]
    )
    assert result.exit_code != 0
