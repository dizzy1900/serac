"""JSON-Schema export of every registered domain contract.

Each module under `serac.domain` may expose `CONTRACTS: dict[str, type[BaseModel]]` mapping a
kebab-case schema name to a model. `discover_contracts` merges them (duplicate names are an
error), `write_contracts` renders `contracts/<name>.v0.json`, and `check_contracts` reports
drift between the registered models and the committed files.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import serac.domain

CONTRACT_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_BASE = "https://github.com/dizzy1900/serac/contracts"
CONTRACT_MAJOR = 0


def contract_filename(name: str) -> str:
    return f"{name}.v{CONTRACT_MAJOR}.json"


def merge_contract_tables(
    tables: Iterable[tuple[str, Mapping[str, Any]]],
) -> dict[str, type[BaseModel]]:
    """Merge `(owner, CONTRACTS)` pairs; reject bad names, non-models and duplicates."""
    found: dict[str, type[BaseModel]] = {}
    owners: dict[str, str] = {}
    for owner, table in tables:
        for name, model in table.items():
            if not re.fullmatch(CONTRACT_NAME_PATTERN, name):
                raise ValueError(f"{owner}: contract name {name!r} is not kebab-case")
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise TypeError(f"{owner}: contract {name!r} is not a pydantic model")
            if name in found:
                raise ValueError(f"duplicate contract name {name!r} in {owner} and {owners[name]}")
            found[name] = model
            owners[name] = owner
    return dict(sorted(found.items()))


def discover_contracts() -> dict[str, type[BaseModel]]:
    """Import every `serac.domain` module and merge its `CONTRACTS` table."""
    tables: list[tuple[str, Mapping[str, Any]]] = []
    for info in sorted(pkgutil.iter_modules(serac.domain.__path__), key=lambda m: m.name):
        module_name = f"{serac.domain.__name__}.{info.name}"
        module = importlib.import_module(module_name)
        table = getattr(module, "CONTRACTS", None)
        if table is None:
            continue
        if not isinstance(table, Mapping):
            raise TypeError(f"{module_name}.CONTRACTS must be a mapping")
        tables.append((module_name, table))
    return merge_contract_tables(tables)


def export_schema(name: str, model: type[BaseModel]) -> dict[str, Any]:
    """Serialization-mode JSON Schema with a stable `$id` and the 2020-12 dialect."""
    schema = model.model_json_schema(mode="serialization")
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = f"{SCHEMA_ID_BASE}/{contract_filename(name)}"
    schema.setdefault("title", model.__name__)
    return schema


def render_schema(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_contracts(out_dir: Path) -> list[Path]:
    """Write `<name>.v0.json` for every registered contract; return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in discover_contracts().items():
        path = out_dir / contract_filename(name)
        path.write_text(render_schema(export_schema(name, model)), encoding="utf-8")
        written.append(path)
    return written


def check_contracts(out_dir: Path) -> list[str]:
    """Names whose committed file is missing or differs from the model; plus stale files."""
    contracts = discover_contracts()
    drift: list[str] = []
    for name, model in contracts.items():
        path = out_dir / contract_filename(name)
        expected = render_schema(export_schema(name, model))
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            drift.append(name)
    suffix = f".v{CONTRACT_MAJOR}.json"
    if out_dir.exists():
        for path in sorted(out_dir.glob(f"*{suffix}")):
            stale = path.name.removesuffix(suffix)
            if stale not in contracts:
                drift.append(stale)
    return drift
