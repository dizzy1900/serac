from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from serac.cli_underwriting import app
from serac.domain.avoided_loss import AvoidedLossStatus, InterventionKind
from serac.domain.forecast import ModelProvenance
from serac.domain.schema_export import write_contracts
from serac.validation.underwriting import (
    FICTIONAL_NOTICE,
    NOT_IMPLEMENTED_MESSAGE,
    example_request,
    not_implemented_response,
    run_underwriting_check,
    schema_errors,
)


def test_example_request_is_labelled_fictional_and_has_baseline() -> None:
    req = example_request()
    assert req.contract_version == "0.0.0"
    assert req.forecast.model.provenance is ModelProvenance.stub
    assert FICTIONAL_NOTICE in req.forecast.assumptions
    assert any(s.intervention is InterventionKind.none for s in req.scenarios)
    assert req.forecast.source_volume_m3.notes == FICTIONAL_NOTICE


def test_response_is_never_computed() -> None:
    resp = not_implemented_response(example_request())
    assert resp.status is AvoidedLossStatus.not_implemented
    assert resp.losses == []
    assert resp.notes == NOT_IMPLEMENTED_MESSAGE


def test_run_check_against_committed_contracts(repo_root: Path) -> None:
    result = run_underwriting_check(repo_root / "contracts")
    assert result.ok, result.failures
    assert len(result.passed) == 5
    assert any("avoided-loss.v0.json" in step for step in result.passed)
    assert any("avoided-loss-response.v0.json" in step for step in result.passed)


def test_run_check_reports_missing_contracts(tmp_path: Path) -> None:
    result = run_underwriting_check(tmp_path)
    assert not result.ok
    assert "run `serac schema export` first" in result.failures[0]


def test_schema_errors_reports_paths(tmp_path: Path) -> None:
    write_contracts(tmp_path)
    req = example_request().model_dump(mode="json")
    req["scenarios"][0]["intervention"] = "wishful"
    schema = __import__("json").loads((tmp_path / "avoided-loss.v0.json").read_text())
    errors = schema_errors(schema, req)
    assert errors and errors[0].startswith("scenarios/0/intervention:")


def test_cli_round_trip_only_still_passes(repo_root: Path) -> None:
    """--no-table is the Prompt 1 behaviour minus the exit 2: the round-trip alone."""
    result = CliRunner().invoke(app, ["--contracts", str(repo_root / "contracts"), "--no-table"])
    assert result.exit_code == 0
    assert "ok: AvoidedLossRequest validates against avoided-loss.v0.json" in result.output
    assert "INPUT PROVENANCE" not in result.output


def test_cli_computes_and_prints_the_table(repo_root: Path) -> None:
    """M5: the command computes rather than exiting 2, and leads with the provenance header."""
    result = CliRunner().invoke(
        app, ["--contracts", str(repo_root / "contracts"), "--repo", str(repo_root)]
    )
    assert result.exit_code == 0, result.output
    assert NOT_IMPLEMENTED_MESSAGE not in result.output
    assert "INPUT PROVENANCE" in result.output
    assert "NO VALIDATED FORECAST EXISTS FOR THIS EVENT" in result.output
    assert "COMPUTED:" in result.output
    # Every asset that could not be costed says so; none of them is reported as a zero.
    assert "UNDETERMINED" in result.output


def test_cli_exits_1_when_contracts_missing(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--contracts", str(tmp_path)])
    assert result.exit_code == 1
    assert NOT_IMPLEMENTED_MESSAGE not in result.output
    assert "FAIL:" in result.output
