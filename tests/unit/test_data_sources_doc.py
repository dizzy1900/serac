"""`docs/DATA_SOURCES.md` is checked against the ledger, not trusted.

The document is the answer to "what does serac read, under what licence, and what is missing".
It went stale once — four ledger sources (Syngine, ESEC, RGI 7.0, simulation outputs) and two
data centres (RESIF, INGV) had no section at all, nine `(planned)` tags pointed at modules and
fixtures that had existed for a while, and two sentences said `h5py` was absent from an
environment that ships it. Every one of those is a claim that a machine can check, so a machine
checks it here rather than a reader noticing.

Three of the families that fix caught are not specific to this document at all — paths resolve,
nothing in the tree is tagged as future work, no false claim that a package is missing — and
`docs/ARCHITECTURE.md` drifted the same three ways one prompt later because the checks lived
here rather than anywhere a second document could reach them. They now live in
`tests/unit/doc_claims.py` and run against every prose document from
`tests/unit/test_docs_consistency.py`.

What remains here is what only a data-source document can be checked for, all of it against
`data/manifest.jsonl`:

1. **Completeness against the ledger.** Every `source` and every `adapter` that appears in
   `data/manifest.jsonl`, and every `DataSource` member, is named in the document. A new
   adapter cannot start writing rows without a section describing what it reads.
2. **Completeness against the tree.** Every module under `adapters/eo`, `adapters/seismic`
   and `adapters/hydro` is either documented or listed in `NOT_A_SOURCE_MODULE` below with a
   reason.
3. **Hosts.** Every host serac has actually retrieved bytes from is named.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.doc_claims import backticked, read_doc

from serac.domain.manifest import DataSource

DOC_PATH = "docs/DATA_SOURCES.md"

# Modules that live beside the adapters but read no external source of their own. Adding a file
# here is the deliberate way to say "this is not a data source"; anything else must be
# documented.
NOT_A_SOURCE_MODULE: dict[str, str] = {
    "src/serac/adapters/eo/s2_cloud.py": "SCL cloud-mask helper shared by both S2 adapters",
    "src/serac/adapters/seismic/obspy_codec.py": "MiniSEED encode/decode, no service behind it",
}

# `source_document` is per-article by construction: the publisher host lives in each ledger row
# and there is no fixed set of them to enumerate in a document.
HOSTS_NOT_ENUMERABLE = {DataSource.source_document.value}


@pytest.fixture(scope="module")
def doc(repo_root: Path) -> str:
    return read_doc(repo_root, DOC_PATH)


@pytest.fixture(scope="module")
def ledger_rows(repo_root: Path) -> list[dict[str, object]]:
    path = repo_root / "data" / "manifest.jsonl"
    assert path.exists(), "data/manifest.jsonl is missing"
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _table_cells(doc: str) -> Iterator[tuple[str, str]]:
    """Yield `(key, value)` for every two-column table row in the document."""
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        parts = [p.strip() for p in stripped.strip("|").split("|")]
        if len(parts) == 2 and set(parts[0]) != {"-"}:
            yield parts[0], parts[1]


def _documented_ledger_sources(doc: str) -> set[str]:
    found: set[str] = set()
    for key, value in _table_cells(doc):
        if key == "Ledger source":
            found.update(backticked(value))
    return found


def test_every_ledger_source_has_a_section(doc: str, ledger_rows: list[dict[str, object]]) -> None:
    """A source that has written a row must be described."""
    in_ledger = {str(row["source"]) for row in ledger_rows}
    missing = sorted(in_ledger - _documented_ledger_sources(doc))
    assert not missing, f"{DOC_PATH} has no `Ledger source` row for: {missing}"


def test_documented_sources_are_real_and_complete(doc: str) -> None:
    """The `Ledger source` rows and `DataSource` are the same set, in both directions."""
    documented = _documented_ledger_sources(doc)
    declared = {member.value for member in DataSource}
    assert not documented - declared, f"{DOC_PATH} names sources that are not in `DataSource`"
    assert not declared - documented, (
        f"{DOC_PATH} documents no `Ledger source` for: {sorted(declared - documented)}"
    )


def test_every_ledger_adapter_is_named(doc: str, ledger_rows: list[dict[str, object]]) -> None:
    """Every writer of ledger rows is named, so a new one cannot arrive undocumented."""
    adapters = {str(row["adapter"]) for row in ledger_rows if row.get("adapter")}
    missing = sorted(name for name in adapters if name not in doc)
    assert not missing, f"{DOC_PATH} does not name these ledger `adapter` values: {missing}"


def test_every_source_adapter_module_is_documented(doc: str, repo_root: Path) -> None:
    """A module under the source-facing adapter packages is documented or explicitly excluded."""
    modules: list[str] = []
    for package in ("eo", "seismic", "hydro"):
        for path in sorted((repo_root / "src" / "serac" / "adapters" / package).glob("*.py")):
            if path.name.startswith("_"):
                continue
            modules.append(path.relative_to(repo_root).as_posix())
    missing = [m for m in modules if m not in doc and m not in NOT_A_SOURCE_MODULE]
    assert not missing, (
        f"{DOC_PATH} does not name these adapter modules: {missing}. Document them, or add "
        "them to NOT_A_SOURCE_MODULE with the reason they read no source."
    )


def test_excluded_modules_still_exist(repo_root: Path) -> None:
    """`NOT_A_SOURCE_MODULE` cannot silently rot into an excuse for a deleted file."""
    for module in NOT_A_SOURCE_MODULE:
        assert (repo_root / module).exists(), f"{module} is excluded here but does not exist"


def test_every_fetched_host_is_named(doc: str, ledger_rows: list[dict[str, object]]) -> None:
    """Every host bytes came from is named — this is what caught the missing RESIF and INGV."""
    missing: set[tuple[str, str]] = set()
    for row in ledger_rows:
        source = str(row["source"])
        url = row.get("url")
        if source in HOSTS_NOT_ENUMERABLE or not isinstance(url, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        host = url.split("//", 1)[1].split("/", 1)[0]
        if host and host not in doc:
            missing.add((source, host))
    assert not missing, (
        f"{DOC_PATH} does not name these hosts serac fetched from: {sorted(missing)}"
    )
