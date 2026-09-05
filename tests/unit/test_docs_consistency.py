"""Every prose document is checked against the tree, not trusted.

`docs/DATA_SOURCES.md` went stale and was fixed with a test (RELEASE_STATUS gap 70). One prompt
later `docs/ARCHITECTURE.md` had gone stale in exactly the same ways — future-work tags on
committed modules, counts that had grown, and a package described as missing from an
environment that ships it — because the fix had been written for one document rather than for
the class of problem (gap 72).

This module closes the class. The three generic families in `doc_claims` run against every
prose document in the repository, and a document added to `docs/` is picked up by the glob
rather than by someone remembering to write a test for it.

Three tiers, and the differences between them are the point:

* `LIVING_DOCS` describe the tree as it is now. All three families apply.
* `DATED_RECORDS` — ADRs and model cards — are dated statements about what was decided or
  measured at a moment. Their paths must still resolve and they may not lie about the
  environment, but they are **not** rewritten to match today's tree: ADR-0005 saying
  `seisbench` was reserved for Prompt 2 is the record of a decision, not a stale claim.
* `LEDGERS` — `RELEASE_STATUS.md` — get the path check only. The ledger's job is to quote what
  a document or a component used to say, including the wording of claims it has since
  corrected: gap 70 exists to record that two sentences called `h5py` absent from an
  environment that ships it. A checker cannot tell a quoted mistake from a fresh one, so
  applying the other two families here would punish the ledger for doing its job.

What none of this checks is whether the prose is *true*. It checks that it is consistent with
the tree, which is the part a machine can own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.doc_claims import (
    broken_paths,
    false_absence_claims,
    read_doc,
    stale_deferrals,
)

LIVING_DOCS = (
    "README.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "docs/ARCHITECTURE.md",
    "docs/CREDENTIALS.md",
    "docs/DATA_SOURCES.md",
    "docs/EVENT_LIBRARY.md",
)

LEDGERS = ("RELEASE_STATUS.md",)

ALL_PATH_CHECKED = LIVING_DOCS + LEDGERS


def _dated_records(repo_root: Path) -> tuple[str, ...]:
    paths = sorted((repo_root / "docs" / "adr").glob("ADR-*.md"))
    paths += sorted((repo_root / "reports").glob("*.md"))
    return tuple(path.relative_to(repo_root).as_posix() for path in paths)


def test_listed_docs_all_exist(repo_root: Path) -> None:
    """The lists above are the contract; a document may not quietly disappear from one."""
    missing = [name for name in ALL_PATH_CHECKED if not (repo_root / name).exists()]
    assert not missing, f"documents listed here do not exist: {missing}"


def test_every_markdown_doc_is_covered(repo_root: Path) -> None:
    """No document under `docs/` escapes the checks by not being listed."""
    on_disk = {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "docs").glob("*.md")
        if path.is_file()
    }
    uncovered = sorted(on_disk - set(LIVING_DOCS))
    assert not uncovered, (
        f"docs/ holds documents no test covers: {uncovered}. Add them to LIVING_DOCS."
    )


@pytest.mark.parametrize("doc_path", ALL_PATH_CHECKED)
def test_cited_repository_paths_exist(repo_root: Path, doc_path: str) -> None:
    broken = broken_paths(repo_root, read_doc(repo_root, doc_path))
    assert not broken, f"{doc_path} cites paths that do not exist: {broken}"


@pytest.mark.parametrize("doc_path", LIVING_DOCS)
def test_no_false_claim_that_a_package_is_missing(repo_root: Path, doc_path: str) -> None:
    """`docs/ARCHITECTURE.md` said NetCDF-4 reads need `h5py`, "not in the lock"; it is."""
    offenders = false_absence_claims(read_doc(repo_root, doc_path))
    assert not offenders, f"{doc_path} says a package is missing that imports: {offenders}"


@pytest.mark.parametrize("doc_path", LIVING_DOCS)
def test_nothing_in_the_tree_is_deferred(repo_root: Path, doc_path: str) -> None:
    """A living document may not promise for later something already in the tree.

    `docs/ARCHITECTURE.md` carried `planned (P2)` on the LFH inversion and the cascade
    surrogate while `src/serac/models/lfh/` and `src/serac/models/runout/` were on `main`, and
    said the latency measurement was Prompt 2's to make after it had been made. A deferral is
    honest only when it names a repository path that is genuinely still missing.
    """
    offenders = stale_deferrals(repo_root, read_doc(repo_root, doc_path))
    assert not offenders, (
        f"{doc_path} defers things that are in the tree: {offenders}. Drop the tag, or name the "
        "repository path that is genuinely missing."
    )


def test_dated_records_cite_paths_that_exist(repo_root: Path) -> None:
    """ADRs and model cards are not rewritten, but their paths must still resolve."""
    broken = {
        doc_path: paths
        for doc_path in _dated_records(repo_root)
        if (paths := broken_paths(repo_root, read_doc(repo_root, doc_path)))
    }
    assert not broken, f"dated records cite paths that do not exist: {broken}"


def test_dated_records_make_no_false_absence_claim(repo_root: Path) -> None:
    offenders = {
        doc_path: found
        for doc_path in _dated_records(repo_root)
        if (found := false_absence_claims(read_doc(repo_root, doc_path)))
    }
    assert not offenders, f"dated records say a package is missing that imports: {offenders}"
