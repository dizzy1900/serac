"""`serac cube build/describe` and the credentialed `serac ingest` commands' dry runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from serac.cli_cube import app as cube_app
from serac.cli_ingest import app as ingest_app

runner = CliRunner()
BBOX = "79.68,30.33,79.80,30.42"


def fake_repo(repo_root: Path, tmp_path: Path) -> Path:
    """A tmp repository with only what the cube needs: fixtures, ledger, synthetic files."""
    fake = tmp_path / "repo"
    shutil.copytree(repo_root / "data" / "fixtures", fake / "data" / "fixtures")
    shutil.copy(repo_root / "data" / "manifest.jsonl", fake / "data" / "manifest.jsonl")
    shutil.copytree(repo_root / "tests" / "fixtures", fake / "tests" / "fixtures")
    return fake


def test_cube_build_dry_run_writes_nothing(repo_root: Path, tmp_path: Path) -> None:
    fake = fake_repo(repo_root, tmp_path)
    result = runner.invoke(
        cube_app,
        [
            "build",
            "--aoi",
            "chamoli-rishiganga",
            "--from",
            "2021-01-01",
            "--to",
            "2021-02-15",
            "--raw-root",
            str(fake / "data" / "fixtures"),
            "--bbox",
            BBOX,
            "--epsg",
            "32644",
            "--dry-run",
            "--data-dir",
            str(fake / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "EPSG:32644" in result.output and "ledger entries would be considered" in result.output
    assert not (fake / "data" / "features").exists() and not (fake / "reports").exists()


def test_cube_build_then_describe(repo_root: Path, tmp_path: Path) -> None:
    fake = fake_repo(repo_root, tmp_path)
    build = runner.invoke(
        cube_app,
        [
            "build",
            "--aoi",
            "chamoli-rishiganga",
            "--from",
            "2021-01-01",
            "--to",
            "2021-02-15",
            "--raw-root",
            str(fake / "data" / "fixtures"),
            "--bbox",
            BBOX,
            "--epsg",
            "32644",
            "--data-dir",
            str(fake / "data"),
        ],
    )
    assert build.exit_code == 0, build.output
    assert "contains_synthetic: true" in build.output
    assert (fake / "data" / "features" / "chamoli-rishiganga" / "cube.zarr" / "zarr.json").exists()
    report = json.loads((fake / "reports" / "cube" / "chamoli-rishiganga.json").read_text("utf-8"))
    assert report["n_times"] == 4
    describe = runner.invoke(
        cube_app, ["describe", "--aoi", "chamoli-rishiganga", "--data-dir", str(fake / "data")]
    )
    assert describe.exit_code == 0, describe.output
    assert "SYNTHETIC placeholder" in describe.output and "not_fetched" in describe.output
    as_json = runner.invoke(
        cube_app,
        ["describe", "--aoi", "chamoli-rishiganga", "--data-dir", str(fake / "data"), "--json"],
    )
    assert as_json.exit_code == 0, as_json.output
    doc = json.loads(as_json.output)
    layers = {row["layer"]: row for row in doc["layers"]}
    assert layers["s1_coherence_t"]["provenance"] == "synthetic"
    assert layers["nisar_hh_t"]["status"] == "not_fetched"
    assert layers["dem"]["status"] == "partial" and len(doc["times"]) == 4
    assert [row["layer"] for row in doc["layers"]][:3] == ["dem", "slope", "aspect"]


def test_cube_build_needs_bbox_without_aoi_files(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    result = runner.invoke(
        cube_app,
        [
            "build",
            "--aoi",
            "nowhere",
            "--from",
            "2021-01-01",
            "--to",
            "2021-01-02",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 2 and "--bbox" in result.output
    missing = runner.invoke(
        cube_app, ["describe", "--aoi", "nowhere", "--data-dir", str(tmp_path / "data")]
    )
    assert missing.exit_code == 2


def test_ingest_dry_runs_write_nothing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    common = ["--aoi", "chamoli-rishiganga", "--bbox", BBOX, "--dry-run", "--data-dir", str(data)]
    era5 = runner.invoke(
        ingest_app, ["era5", "--from", "2021-02-05", "--to", "2021-02-06", *common]
    )
    assert era5.exit_code == 0, era5.output
    assert "CDS API key" in era5.output and "grid points" in era5.output
    gacos = runner.invoke(
        ingest_app, ["gacos", "--date", "20210130", "--date", "20210211", *common]
    )
    assert gacos.exit_code == 0, gacos.output
    assert "GACOS" in gacos.output
    no_dates = runner.invoke(ingest_app, ["gacos", *common])
    assert no_dates.exit_code == 2
    assert not data.exists()
    usage = runner.invoke(
        ingest_app,
        ["gacos", "--receive", "https://x.invalid/a.tar.gz", *common[:4], "--data-dir", str(data)],
    )
    assert usage.exit_code == 2  # --receive needs --request-id


def test_ingest_gacos_request_without_email_exits_credentials(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GACOS_EMAIL", raising=False)
    from serac.settings import get_settings

    get_settings.cache_clear()
    data = tmp_path / "data"
    result = runner.invoke(
        ingest_app,
        [
            "gacos",
            "--aoi",
            "chamoli-rishiganga",
            "--bbox",
            BBOX,
            "--date",
            "20210130",
            "--yes",
            "--data-dir",
            str(data),
        ],
    )
    assert result.exit_code == 3, result.output
    rows = (data / "manifest.jsonl").read_text("utf-8").splitlines()
    assert len(rows) == 1 and '"status":"not_fetched"' in rows[0]
