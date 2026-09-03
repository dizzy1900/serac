"""dvc.yaml parses, names the expected stages, freezes credentialed ingests, no ledger out."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

AOIS = ("lhende-khola-trishuli", "chamoli-rishiganga", "blatten-lotschental")


@pytest.fixture(scope="module")
def stages(repo_root: Path) -> dict[str, dict[str, object]]:
    doc = yaml.safe_load((repo_root / "dvc.yaml").read_text("utf-8"))
    assert isinstance(doc, dict) and "stages" in doc
    stages: dict[str, dict[str, object]] = doc["stages"]
    return stages


def test_stage_names(stages: dict[str, dict[str, object]]) -> None:
    for aoi in AOIS:
        assert f"ingest_dem_{aoi}" in stages
        assert f"build_cube_{aoi}" in stages
        assert f"ingest_hyp3_{aoi}" in stages and f"ingest_era5_{aoi}" in stages
    assert "ingest_nisar_lhende-khola-trishuli" in stages


def test_credentialed_ingests_are_frozen(stages: dict[str, dict[str, object]]) -> None:
    for name, stage in stages.items():
        if name.startswith(("ingest_hyp3_", "ingest_era5_", "ingest_nisar_")):
            assert stage.get("frozen") is True, name
        if name.startswith(("ingest_dem_", "ingest_s2_", "build_cube_")):
            assert stage.get("frozen") is not True, name


def test_ledger_is_never_an_output(stages: dict[str, dict[str, object]]) -> None:
    for name, stage in stages.items():
        outs = stage.get("outs", [])
        assert isinstance(outs, list)
        for out in outs:
            path = out if isinstance(out, str) else next(iter(out))
            assert "manifest.jsonl" not in path, name
            assert path.startswith(("data/raw/", "data/features/")), (name, path)


def test_cube_reports_are_uncached_metrics(stages: dict[str, dict[str, object]]) -> None:
    for aoi in AOIS:
        metrics = stages[f"build_cube_{aoi}"]["metrics"]
        assert isinstance(metrics, list) and len(metrics) == 1
        entry = metrics[0]
        assert isinstance(entry, dict)
        path, opts = next(iter(entry.items()))
        assert path == f"reports/cube/{aoi}.json" and opts == {"cache": False}
        assert "--epsg" in str(stages[f"build_cube_{aoi}"]["cmd"])


def test_dvc_config_has_no_remote_url(repo_root: Path) -> None:
    config = (repo_root / ".dvc" / "config").read_text("utf-8")
    assert "autostage = true" in config and "url" not in config
    assert "/config.local" in (repo_root / ".dvc" / ".gitignore").read_text("utf-8")
    assert (repo_root / ".dvcignore").exists()
    data_ignore = (repo_root / "data" / ".gitignore").read_text("utf-8")
    for sub in ("/raw/*", "/interim/*", "/features/*"):
        assert sub in data_ignore


@pytest.mark.slow
def test_dvc_stage_list_runs(repo_root: Path) -> None:
    """`uv run dvc stage list` is what CI runs to prove the pipeline parses."""
    result = subprocess.run(
        [sys.executable, "-m", "dvc", "stage", "list"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={"DVC_NO_ANALYTICS": "1", "PATH": "", "HOME": str(repo_root)},
    )
    assert result.returncode == 0, result.stderr
    for aoi in AOIS:
        assert f"build_cube_{aoi}" in result.stdout
