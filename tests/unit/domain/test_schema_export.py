from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from serac.cli_schema import app
from serac.domain.avoided_loss import AvoidedLossRequest
from serac.domain.events import MassMovementEvent
from serac.domain.schema_export import (
    SCHEMA_DIALECT,
    SCHEMA_ID_BASE,
    check_contracts,
    contract_filename,
    discover_contracts,
    export_schema,
    merge_contract_tables,
    render_schema,
    write_contracts,
)

EXPECTED_NAMES = {
    "manifest-entry",
    "source-ref",
    "mass-movement-event",
    "aoi",
    "grid-spec",
    "slope-unit",
    "transect",
    "exposed-asset",
    "cascade-forecast",
    "avoided-loss",
    "avoided-loss-response",
}


class Dummy(BaseModel):
    x: int


def test_discover_contracts_merges_every_domain_module() -> None:
    contracts = discover_contracts()
    assert set(contracts) >= EXPECTED_NAMES
    assert list(contracts) == sorted(contracts)
    assert contracts["mass-movement-event"] is MassMovementEvent
    assert contracts["avoided-loss"] is AvoidedLossRequest


def test_merge_rejects_duplicates_bad_names_and_non_models() -> None:
    with pytest.raises(ValueError, match="duplicate contract name 'dummy' in b and a"):
        merge_contract_tables([("a", {"dummy": Dummy}), ("b", {"dummy": Dummy})])
    with pytest.raises(ValueError, match="not kebab-case"):
        merge_contract_tables([("a", {"Dummy_Model": Dummy})])
    with pytest.raises(TypeError, match="not a pydantic model"):
        merge_contract_tables([("a", {"dummy": object})])
    merged = merge_contract_tables([("b", {"z-2": Dummy}), ("a", {"a-1": Dummy})])
    assert list(merged) == ["a-1", "z-2"]


def test_export_schema_sets_id_dialect_and_title() -> None:
    schema = export_schema("dummy", Dummy)
    assert schema["$schema"] == SCHEMA_DIALECT
    assert schema["$id"] == f"{SCHEMA_ID_BASE}/dummy.v0.json"
    assert schema["title"] == "Dummy"
    assert schema["properties"]["x"]["type"] == "integer"


def test_render_schema_is_sorted_indented_and_newline_terminated() -> None:
    text = render_schema({"b": 1, "a": {"d": 2, "c": 3}})
    assert text == '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n'


def test_write_then_check_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "contracts"
    written = write_contracts(out)
    assert {p.name for p in written} == {contract_filename(n) for n in discover_contracts()}
    assert check_contracts(out) == []
    for path in written:
        json.loads(path.read_text(encoding="utf-8"))


def test_check_reports_modified_missing_and_stale(tmp_path: Path) -> None:
    out = tmp_path / "contracts"
    write_contracts(out)
    (out / contract_filename("aoi")).write_text("{}\n", encoding="utf-8")
    (out / contract_filename("transect")).unlink()
    (out / contract_filename("legacy-thing")).write_text("{}\n", encoding="utf-8")
    assert check_contracts(out) == ["aoi", "transect", "legacy-thing"]


def test_check_on_missing_directory_lists_everything(tmp_path: Path) -> None:
    assert set(check_contracts(tmp_path / "nowhere")) == set(discover_contracts())


def test_cli_export_writes_and_check_passes(tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "c"
    result = runner.invoke(app, ["export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert str(out / contract_filename("aoi")) in result.output
    result = runner.invoke(app, ["export", "--out", str(out), "--check"])
    assert result.exit_code == 0, result.output
    assert "contracts up to date" in result.output


def test_cli_check_exits_1_on_drift(tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "c"
    write_contracts(out)
    (out / contract_filename("aoi")).write_text("{}\n", encoding="utf-8")
    result = runner.invoke(app, ["export", "--out", str(out), "--check"])
    assert result.exit_code == 1
    assert "contract drift" in result.output
    assert "aoi" in result.output


def test_cli_no_args_shows_help() -> None:
    result = CliRunner().invoke(app, [])
    assert "export" in result.output
