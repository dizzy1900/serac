"""The committed contracts/ files must equal what the registered models generate."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from serac.domain.schema_export import (
    SCHEMA_DIALECT,
    check_contracts,
    contract_filename,
    discover_contracts,
)


def test_committed_contracts_match_models(repo_root: Path) -> None:
    drift = check_contracts(repo_root / "contracts")
    assert drift == [], f"contract drift: {drift}; run `serac schema export` and commit"


def test_every_contract_is_a_valid_2020_12_schema(repo_root: Path) -> None:
    for name in discover_contracts():
        path = repo_root / "contracts" / contract_filename(name)
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == SCHEMA_DIALECT
        assert schema["$id"].endswith(f"/contracts/{contract_filename(name)}")
        Draft202012Validator.check_schema(schema)


def test_public_avoided_loss_contract_is_pinned_to_0_0_0(repo_root: Path) -> None:
    schema = json.loads((repo_root / "contracts" / "avoided-loss.v0.json").read_text())
    assert schema["properties"]["contract_version"]["const"] == "0.0.0"
