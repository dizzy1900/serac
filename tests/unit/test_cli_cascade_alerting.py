"""The two M5 sub-apps, exercised as `serac.cli` would mount them.

`serac.cli` is owned by the orchestrator, so these tests drive `cli_cascade.app` and
`cli_alerting.app` directly. If the mounting lines land as reported, `serac cascade …` and
`serac alerting …` behave exactly as asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from serac.cli_alerting import app as alerting_app
from serac.cli_cascade import app as cascade_app

runner = CliRunner()


def test_keygen_writes_a_keypair_and_prints_no_private_material(tmp_path: Path) -> None:
    private = tmp_path / "cap.pem"
    result = runner.invoke(alerting_app, ["keygen", "--out", str(private)])
    assert result.exit_code == 0, result.output
    assert private.exists()
    assert (tmp_path / "cap.pub.pem").exists()
    assert "PRIVATE KEY" not in result.output
    assert private.read_text(encoding="utf-8") not in result.output
    assert "fingerprint : sha256:" in result.output
    assert "SERAC_CAP_SIGNING_KEY" in result.output


def test_keygen_refuses_to_clobber_an_existing_key(tmp_path: Path) -> None:
    private = tmp_path / "cap.pem"
    assert runner.invoke(alerting_app, ["keygen", "--out", str(private)]).exit_code == 0
    again = runner.invoke(alerting_app, ["keygen", "--out", str(private)])
    assert again.exit_code == 2
    assert "refusing to overwrite" in again.output


def test_cap_then_verify_round_trips(tmp_path: Path, repo_root: Path) -> None:
    private = tmp_path / "cap.pem"
    runner.invoke(alerting_app, ["keygen", "--out", str(private)])
    out_dir = tmp_path / "cap"
    built = runner.invoke(
        alerting_app,
        [
            "cap",
            "--repo",
            str(repo_root),
            "--out-dir",
            str(out_dir),
            "--sign",
            "--signing-key",
            str(private),
        ],
    )
    assert built.exit_code == 0, built.output
    assert "status     : Test" in built.output
    assert "signed     : True" in built.output
    assert "FICTIONAL check forecast" in built.output

    written = next(p for p in out_dir.iterdir() if p.suffix == ".xml")
    checked = runner.invoke(
        alerting_app,
        [
            "verify",
            str(written),
            "--repo",
            str(repo_root),
            "--public-key",
            str(tmp_path / "cap.pub.pem"),
        ],
    )
    assert checked.exit_code == 0, checked.output
    assert "signature  : VALID" in checked.output
    assert "xsd        : valid" in checked.output


def test_verify_exits_nonzero_on_an_unsigned_message(tmp_path: Path, repo_root: Path) -> None:
    out_dir = tmp_path / "cap"
    runner.invoke(
        alerting_app, ["cap", "--repo", str(repo_root), "--out-dir", str(out_dir), "--no-sign"]
    )
    written = next(p for p in out_dir.iterdir() if p.suffix == ".xml")
    result = runner.invoke(alerting_app, ["verify", str(written), "--repo", str(repo_root)])
    assert result.exit_code == 1
    assert "ABSENT" in result.output


def test_the_http_sink_needs_an_endpoint(tmp_path: Path, repo_root: Path) -> None:
    result = runner.invoke(
        alerting_app,
        ["cap", "--repo", str(repo_root), "--sink", "http", "--out-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "no default" in result.output


def test_cascade_e2e_prints_where_the_chain_stops(tmp_path: Path, repo_root: Path) -> None:
    result = runner.invoke(
        cascade_app,
        [
            "e2e",
            "--repo",
            str(repo_root),
            "--event",
            "langtang-lhende-2026",
            "--reports-dir",
            str(tmp_path),
            "--no-execute-lfh",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "The chain stops at 'detection'" in result.output
    assert "No cascade forecast and no CAP alert exist" in result.output
    assert (tmp_path / "langtang-lhende-2026.md").exists()


def test_cascade_e2e_refuses_an_unknown_event(repo_root: Path) -> None:
    result = runner.invoke(cascade_app, ["e2e", "--repo", str(repo_root), "--event", "nope"])
    assert result.exit_code == 2
    assert "unknown e2e event" in result.output


def test_cascade_underwriting_table_prints_the_header(repo_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "response.json"
    result = runner.invoke(
        cascade_app, ["underwriting-table", "--repo", str(repo_root), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "INPUT PROVENANCE" in result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "not_implemented"
    assert payload["by_asset"], "the sidecar per-asset table must be written"


def test_cascade_avoided_loss_evaluates_a_supplied_request(repo_root: Path, tmp_path: Path) -> None:
    from serac.alerting.example import check_request

    request_path = tmp_path / "request.json"
    request_path.write_text(check_request().model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "response.json"
    result = runner.invoke(
        cascade_app,
        [
            "avoided-loss",
            "--repo",
            str(repo_root),
            "--request",
            str(request_path),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "computed"
    assert payload["losses"]
    assert "NOT part of contract 0.0.0" in result.output
