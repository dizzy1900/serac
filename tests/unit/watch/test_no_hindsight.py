"""Mechanical proof that the scoring path cannot see the answer.

`reports/watch/PREREGISTRATION.md` section 7 says the Chamoli detachment location may be used
only for post-hoc labelling, and that nothing in the anomaly, scoring or threshold code may
read it. That is a claim about the code, so it is checked against the code: by walking the
transitive import graph of the scoring modules, and by reading their source for the names of
the files and concepts that would give the game away.

These tests are deliberately blunt. A grep-based guard is easy to defeat on purpose; its job
is to fail loudly when someone adds such a read *by accident*, which is how hindsight actually
gets into a model.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from serac.models.watch import anomaly

SCORING_MODULES = ("serac.models.watch.anomaly",)

FORBIDDEN_SUBSTRINGS = (
    "source_zone",
    "detachment",
    "failure_date",
    "failure_time",
    "event_id",
    "chamoli",
    "langtang",
    "ronti",
    "data/events",
    "events.parquet",
)

FORBIDDEN_IMPORTS = (
    "serac.domain.events",
    "serac.models.watch.backtest",
    "geopandas",
    "fiona",
)


def _module_path(module_name: str) -> Path:
    import importlib

    return Path(inspect.getsourcefile(importlib.import_module(module_name)) or "")


def _transitive_serac_imports(module_name: str, seen: set[str] | None = None) -> set[str]:
    """Every `serac.*` module reachable from `module_name` by a static import."""
    import importlib

    seen = seen if seen is not None else set()
    if module_name in seen:
        return seen
    seen.add(module_name)
    try:
        source = _module_path(module_name).read_text(encoding="utf-8")
    except (OSError, ImportError, TypeError):  # pragma: no cover - defensive
        return seen
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            if name.startswith("serac."):
                importlib.import_module(name)
                _transitive_serac_imports(name, seen)
            else:
                seen.add(name)
    return seen


def test_the_scoring_module_source_never_mentions_the_answer() -> None:
    source = _module_path("serac.models.watch.anomaly").read_text(encoding="utf-8").lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        assert needle not in source, (
            f"{needle!r} appears in anomaly.py; the scoring path must not know where or when "
            "a failure happened"
        )


def test_the_scoring_module_imports_nothing_that_could_reveal_the_answer() -> None:
    reachable = _transitive_serac_imports("serac.models.watch.anomaly")
    for forbidden in FORBIDDEN_IMPORTS:
        assert forbidden not in reachable, (
            f"anomaly.py transitively imports {forbidden}, which can read the event library or "
            "the source-zone outline"
        )


def test_the_scoring_module_reads_no_file_at_all() -> None:
    """No `open`, no `read_text`, no `read_file`: the scorer takes arrays, not paths."""
    source = _module_path("serac.models.watch.anomaly").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    for name in ("open", "read_text", "read_bytes", "read_parquet", "read_file", "load", "loads"):
        assert name not in called, f"anomaly.py calls {name}(); the scorer must not read files"


def test_the_tier_vocabulary_contains_nothing_that_implies_a_date_or_a_probability() -> None:
    values = {t.value for t in anomaly.Tier}
    assert values == {"quiet", "elevated", "watch", "insufficient_data"}
    banned = ("imminent", "days", "probability", "forecast", "predict", "eta", "when")
    for value in values:
        assert not any(b in value for b in banned)


def test_public_scoring_functions_take_no_path_arguments() -> None:
    for name, obj in vars(anomaly).items():
        if name.startswith("_") or not callable(obj) or not inspect.isfunction(obj):
            continue
        signature = inspect.signature(obj)
        for parameter in signature.parameters.values():
            annotation = str(parameter.annotation)
            assert "Path" not in annotation, (
                f"anomaly.{name} takes a Path parameter {parameter.name!r}; the scoring path "
                "must be fed arrays by its caller"
            )


def test_the_backtest_module_is_the_only_place_that_labels_the_failed_unit() -> None:
    """The labelling function exists, is documented as post-hoc, and lives outside the scorer."""
    from serac.models.watch import backtest

    assert hasattr(backtest, "failed_unit_id")
    doc = backtest.failed_unit_id.__doc__ or ""
    assert "post-hoc" in doc.lower()
    assert not hasattr(anomaly, "failed_unit_id")


@pytest.mark.parametrize("module_name", SCORING_MODULES)
def test_scoring_modules_declare_the_not_a_prediction_disclaimer(module_name: str) -> None:
    source = _module_path(module_name).read_text(encoding="utf-8").lower()
    assert "not a probability" in source
    assert "never a failure date" in source
