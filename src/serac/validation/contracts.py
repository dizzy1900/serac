"""`validate-contracts`: the committed JSON Schemas match the models and are valid 2020-12.

Equivalent to `serac schema export --check` plus a `Draft202012Validator.check_schema` pass
over every `contracts/*.v0.json`, expressed as a `Suite` so the harness can report it.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from serac.domain.schema_export import (
    CONTRACT_MAJOR,
    SCHEMA_DIALECT,
    check_contracts,
    contract_filename,
    discover_contracts,
)
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "contracts"


def run_suite(repo: Path) -> SuiteResult:
    suite = Suite(SUITE_NAME, repo)
    contracts_dir = repo / "contracts"
    suite.check("contracts_dir_exists", contracts_dir.is_dir(), str(contracts_dir))
    if not contracts_dir.is_dir():
        return suite.result()

    drift = check_contracts(contracts_dir)
    suite.check(
        "schema_export_check",
        not drift,
        "up to date" if not drift else "drift in: " + ", ".join(drift),
    )

    registered = discover_contracts()
    suite.info("contracts_registered", f"{len(registered)} contracts")
    for name in registered:
        path = contracts_dir / contract_filename(name)
        if not path.exists():
            suite.check(f"schema_valid:{name}", False, f"{path} missing")
            continue
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except (ValueError, SchemaError) as exc:
            suite.check(f"schema_valid:{name}", False, f"{path}: {exc}")
            continue
        dialect_ok = schema.get("$schema") == SCHEMA_DIALECT
        id_ok = str(schema.get("$id", "")).endswith(f"/contracts/{contract_filename(name)}")
        suite.check(
            f"schema_valid:{name}",
            dialect_ok and id_ok,
            "valid Draft 2020-12"
            if dialect_ok and id_ok
            else f"$schema={schema.get('$schema')!r} $id={schema.get('$id')!r}",
        )

    stale = [
        p.name
        for p in sorted(contracts_dir.glob(f"*.v{CONTRACT_MAJOR}.json"))
        if p.name.removesuffix(f".v{CONTRACT_MAJOR}.json") not in registered
    ]
    suite.check("no_stale_contract_files", not stale, ", ".join(stale) or "none")
    return suite.result()
