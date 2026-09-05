"""`docs/ARCHITECTURE.md` is checked against the tree, the CLI and the ledger, not trusted.

The document is the answer to "what is this system made of, and what state is each part in".
It was written on 2026-09-03 and not revised when the five Prompt 2 components merged, so by
2026-09-04 it said: that the LFH inversion and the cascade surrogate were `planned (P2)` while
`src/serac/models/lfh/` and `src/serac/models/runout/` were on `main`; that there were 18
contracts when there were 22, and 6 validation suites when there were 11; that `ForceHistory`
was interfaces only and avoided loss would be "populated in P2" after both had been populated;
that `h5py` was not in the lock, which ships it; that HyP3 had been exercised with a fake only,
against 4,136 real ledger rows; and that Prompt 2 owned a latency measurement Prompt 2 had
made. Nine whole packages had no row at all.

Every one of those is a claim a machine can check, so a machine checks it here. The three
generic families — paths resolve, no false absence claims, nothing in the tree is deferred —
run against this document from `tests/unit/test_docs_consistency.py`. What is specific to an
architecture document is added on top:

1. **Component coverage.** Every package under `src/serac/` is named. A new package cannot
   arrive without a row describing it.
2. **CLI coverage.** Every command mounted on the `serac` app is named.
3. **Counts are derived, not remembered.** The contract and suite counts the document states
   are compared against `contracts/` and `REQUIRED_SUITES`.
4. **Statuses come from the legend.** A `Status` cell may only carry a tag the legend defines,
   so a status cannot be invented mid-table and the legend cannot rot.
5. **No stale "with fakes only" claim.** A component the ledger shows has fetched real bytes
   may not be described as exercised against a fake.
6. **Measured latencies match their report.** The numbers in the latency section are compared
   against `reports/m1/latency_chamoli-2021.json`, so the section cannot drift from the
   measurement it cites.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.doc_claims import backticked, claims, read_doc, repo_paths, table_rows, tables

from serac.cli import app
from serac.validation.promote import REQUIRED_SUITES

DOC_PATH = "docs/ARCHITECTURE.md"

M1_LATENCY_REPORT = "reports/m1/latency_chamoli-2021.json"

# A claim carrying one of these says a component has only ever been run against a double.
FAKE_ONLY_MARKERS = ("with a fake", "with fakes", "fake ", "fakeredis")

# Packages that hold no component of their own: `__init__` re-exports and nothing else. Adding
# a package here is the deliberate way to say "there is nothing here to describe"; anything
# else needs a row.
NOT_A_COMPONENT_PACKAGE: dict[str, str] = {}


@pytest.fixture(scope="module")
def doc(repo_root: Path) -> str:
    return read_doc(repo_root, DOC_PATH)


def _packages(repo_root: Path) -> list[str]:
    root = repo_root / "src" / "serac"
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_dir() or path.name == "__pycache__":
            continue
        out.append(path.relative_to(repo_root).as_posix())
    return out


def _cli_command_names() -> list[str]:
    names = [group.name for group in app.registered_groups if group.name]
    names += [command.name for command in app.registered_commands if command.name]
    return sorted(names)


def _legend_tags(doc: str) -> set[str]:
    """The tags defined by the legend, which is the `Tag | Meaning` table."""
    tags: set[str] = set()
    for header, body in tables(doc):
        if header[:2] != ["Tag", "Meaning"]:
            continue
        for cells in body:
            tags.update(backticked(cells[0]))
    return tags


def _status_cells(doc: str) -> Iterator[tuple[str, str]]:
    """Yield `(row name, status cell)` for every table whose last column is `Status`."""
    for header, body in tables(doc):
        if header[-1] != "Status":
            continue
        for cells in body:
            yield cells[0], cells[-1]


def test_every_package_is_described(repo_root: Path, doc: str) -> None:
    """A package in the tree has a row. This is what `models/`, `cascade/` and `alerting/`
    needed and did not have."""
    missing = [
        package
        for package in _packages(repo_root)
        if package not in doc and package not in NOT_A_COMPONENT_PACKAGE
    ]
    assert not missing, (
        f"{DOC_PATH} does not name these packages: {missing}. Describe them, or add them to "
        "NOT_A_COMPONENT_PACKAGE with the reason there is nothing to describe."
    )


def test_excluded_packages_still_exist(repo_root: Path) -> None:
    """`NOT_A_COMPONENT_PACKAGE` cannot rot into an excuse for a deleted package."""
    for package in NOT_A_COMPONENT_PACKAGE:
        assert (repo_root / package).exists(), f"{package} is excluded here but does not exist"


def test_every_cli_command_is_named(doc: str) -> None:
    """`serac --help` and the document agree on what the entrypoint offers."""
    missing = [name for name in _cli_command_names() if f"`{name}`" not in doc and name not in doc]
    assert not missing, (
        f"{DOC_PATH} does not name these `serac` commands: {missing}. The CLI row in section 2 "
        "is the list a reader checks against `serac --help`."
    )


def test_named_commands_are_real(doc: str) -> None:
    """The other direction: the CLI row may not advertise a command that does not exist."""
    real = set(_cli_command_names())
    row = next((cells for cells in table_rows(doc) if cells[0].startswith("CLI `serac`")), None)
    assert row is not None, f"{DOC_PATH} has no CLI row in the container table"
    advertised = {token for token in backticked(row[1]) if "/" not in token and " " not in token}
    unknown = sorted(advertised - real)
    assert not unknown, f"{DOC_PATH} advertises `serac` commands that do not exist: {unknown}"


def test_declared_contract_count_matches_the_directory(repo_root: Path, doc: str) -> None:
    """The document said 18 while `serac schema export --check` printed 22."""
    on_disk = len(list((repo_root / "contracts").glob("*.json")))
    assert f"({on_disk} contracts)" in doc, (
        f"{DOC_PATH} must state `({on_disk} contracts)`; contracts/ holds {on_disk} schemas"
    )


def test_declared_suite_count_matches_the_harness(doc: str) -> None:
    """The document said 6 while the harness required 11."""
    count = len(REQUIRED_SUITES)
    assert f"({count} suites)" in doc, (
        f"{DOC_PATH} must state `({count} suites)`; REQUIRED_SUITES has {count}"
    )


def test_every_validation_suite_is_named(doc: str) -> None:
    missing = [name for name in REQUIRED_SUITES if f"validate-{name}" not in doc]
    assert not missing, f"{DOC_PATH} does not name these validation suites: {missing}"


def test_status_cells_use_the_legend(doc: str) -> None:
    """A `Status` cell carries a legend tag, so statuses cannot be invented row by row."""
    tags = _legend_tags(doc)
    assert tags, f"{DOC_PATH} has no status legend"
    offenders = [
        (name, cell)
        for name, cell in _status_cells(doc)
        if not any(cell.startswith(f"`{tag}`") for tag in tags)
    ]
    assert not offenders, (
        f"{DOC_PATH} has Status cells that do not start with a legend tag {sorted(tags)}: "
        f"{offenders}"
    )


def _real_ledger_names(repo_root: Path) -> set[str]:
    """`source` and `adapter` values that have retrieved or requested real bytes."""
    names: set[str] = set()
    with (repo_root / "data" / "manifest.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("provenance") != "real" or row.get("status") not in {"fetched", "requested"}:
                continue
            names.add(str(row["source"]))
            if row.get("adapter"):
                names.add(str(row["adapter"]))
    return names


def test_no_fake_only_claim_for_a_component_with_real_rows(repo_root: Path, doc: str) -> None:
    """HyP3 was described as "exercised with a fake HyP3 only" against 4,136 real rows.

    A claim that something has only ever met a double is checked against the ledger: if the
    module it names has written a `real` row, the claim is false.
    """
    real = _real_ledger_names(repo_root)
    offenders: list[tuple[str, str]] = []
    for claim in claims(doc):
        lowered = claim.lower()
        if not any(marker in lowered for marker in FAKE_ONLY_MARKERS):
            continue
        for token in repo_paths(claim):
            stem = Path(token).stem
            if stem in real or any(name.startswith(stem) for name in real):
                offenders.append((stem, claim))
    assert not offenders, (
        f"{DOC_PATH} says these were exercised against fakes only, but the ledger holds real "
        f"rows for them: {offenders}"
    )


def test_measured_latencies_match_the_report(repo_root: Path, doc: str) -> None:
    """Section 4.3 quotes the measured detection latency; the numbers come from the report.

    The section used to say Prompt 2 owned the measurement. It has been made
    (`reports/m1/latency_chamoli-2021.json`, RELEASE_STATUS gap 20), and quoting it means the
    document has to move when the measurement does.
    """
    report = json.loads((repo_root / M1_LATENCY_REPORT).read_text(encoding="utf-8"))
    assert M1_LATENCY_REPORT in doc, f"{DOC_PATH} must cite {M1_LATENCY_REPORT}"
    missing: list[str] = []
    for mode in report["modes"]:
        for key in ("stream_latency_s", "theoretical_floor_s"):
            value = mode[key]
            if value is None:
                continue
            if f"{round(float(value))} s" not in doc:
                missing.append(f"{mode['mode']}.{key} = {round(float(value))} s")
    assert not missing, (
        f"{DOC_PATH} does not carry the measured figures from {M1_LATENCY_REPORT}: {missing}"
    )
    budget = report["brief_budget_s"]
    assert f"{round(float(budget))} s" in doc, (
        f"{DOC_PATH} must state the brief's {round(float(budget))} s detection budget"
    )
