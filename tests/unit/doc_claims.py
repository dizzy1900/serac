"""Machinery for checking a prose document against the tree it describes.

Two documents have now gone stale in the same way. `docs/DATA_SOURCES.md` described a tree that
had moved on (RELEASE_STATUS gap 70) and `docs/ARCHITECTURE.md` did the same one prompt later:
rows tagged as future work beside modules that had been committed for weeks, counts of things
that had grown, and a claim that a package was missing from an environment that ships it.

The lesson of the first fix was that these are claims a machine can check. The lesson of the
second is that checking them in one test module, for one document, only protects that document.
So the generic families live here, `tests/unit/test_docs_consistency.py` applies them to every
prose document in the repository, and a per-document test module adds whatever is specific to
that document on top.

Three families are generic, because they are ways *any* document can drift:

1. **Paths resolve.** Every repository path a document cites exists.
2. **No false absence claims.** A document may not say the environment lacks a package that
   imports.
3. **No stale deferrals.** A document may not describe something as future work while it is in
   the tree. A deferral has to name a repository path that is genuinely missing.

Family 3 applies only to documents that describe the present. An ADR is a dated decision and a
model card is a dated measurement; both are meant to say what was true when they were written,
and rewriting them to match today's tree would destroy the record. `test_docs_consistency.py`
draws that line, not this module.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path

# Roots that git does not carry (DVC-tracked; see `.gitignore`). A path under one of these is
# absent on a fresh clone even when the claim that names it is correct, so its existence is not
# checkable either way: it can neither fail family 1 nor satisfy a deferral in family 3.
UNCHECKABLE_ROOTS = ("data/raw/", "data/interim/", "data/features/")

REPO_PREFIXES = (
    "src/",
    "tests/",
    "data/",
    "docs/",
    "contracts/",
    "infra/",
    "scripts/",
    "reports/",
    "baselines/",
)

# Phrases that are about the installed environment on their own. `not in the lock` is here
# because that is the form the false `h5py` claim took in `docs/ARCHITECTURE.md`.
LOCKFILE_MARKERS = (
    "not in the lock",
    "not in uv.lock",
    "is not installed",
    "does not ship",
)

# Looser words that only mean an absence *from the environment* when the claim says so. Kept
# separate because "not available" and "absent" are ordinary words about data and services.
ABSENCE_MARKERS = ("absent", "does not ship", "is not installed", "not available", "lacks")

# Words that promise something for later. A claim carrying one of these is only honest if it
# names a repository path that does not exist yet.
DEFERRAL_MARKERS = (
    r"\bplanned\b",
    r"\(P[0-9]\)",
    r"\bin P[0-9]\b",
    r"\breserved for Prompt [0-9]\b",
    r"\bdecided in Prompt [0-9]\b",
    r"\breplaced in Prompt [0-9]\b",
    r"\bPrompt [0-9] owns\b",
    r"\bfor Prompt [0-9]\b",
    r"\bcomes? later\b",
    r"\bto be decided\b",
    r"\byet to be\b",
)

BACKTICKED = re.compile(r"`([^`]+)`")
MODULE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
DEFERRAL = re.compile("|".join(DEFERRAL_MARKERS), re.IGNORECASE)
LIST_ITEM = re.compile(r"^(?:[-*+]\s|\d+\.\s|>\s|#)")

# Tokens that are patterns or elisions rather than paths: `infra/jobs/*.yaml`,
# `src/serac/adapters/{eo,seismic}/`, `reports/promotion/71f3426c….json`.
_NOT_A_LITERAL_PATH = ("*", "{", "}", "<", ">", "…", " ", ",", "?")


def read_doc(repo_root: Path, relative_path: str) -> str:
    path = repo_root / relative_path
    assert path.exists(), f"{relative_path} is missing"
    return path.read_text(encoding="utf-8")


def claims(doc: str) -> Iterator[str]:
    """Yield the units a claim can live in: a table row, a list item, or a paragraph.

    Granularity is the whole difficulty. Too coarse and one word poisons a page: a single
    `planned` in a five-bullet list would condemn the four honest bullets with it. Too fine and
    a claim is split from its subject, because the prose wraps at 95 columns and the two are
    routinely on different lines. A markdown list item — including a numbered one, which is how
    `RELEASE_STATUS.md` writes a gap — therefore starts a new unit and continues across its own
    continuation lines. Fenced blocks are skipped entirely: an ASCII diagram is labels, not
    assertions.
    """
    unit: list[str] = []
    fenced = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if unit:
                yield " ".join(unit)
                unit = []
            fenced = not fenced
            continue
        if fenced:
            continue
        is_table = stripped.startswith("|") and stripped.endswith("|")
        if is_table or not stripped or LIST_ITEM.match(stripped):
            if unit:
                yield " ".join(unit)
                unit = []
            if is_table:
                yield stripped
                continue
            if not stripped:
                continue
        unit.append(stripped)
    if unit:
        yield " ".join(unit)


def _is_separator(cells: list[str]) -> bool:
    return all(cell and set(cell) <= {"-", ":"} for cell in cells)


def tables(doc: str) -> Iterator[tuple[list[str], list[list[str]]]]:
    """Yield `(header cells, body rows)` for every markdown table.

    Tables are yielded whole rather than row by row because a check on a column — "the Status
    column may only carry a legend tag" — needs to know which table a row belongs to, and the
    separator line is the only thing that marks where one table ends and the next begins.
    """
    header: list[str] | None = None
    pending: list[str] | None = None
    body: list[list[str]] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            if header is not None:
                yield header, body
            header, pending, body = None, None, []
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if _is_separator(cells):
            if pending is not None:
                if header is not None:
                    yield header, body
                header, body, pending = pending, [], None
            continue
        if header is None:
            pending = cells
        else:
            body.append(cells)
    if header is not None:
        yield header, body


def table_rows(doc: str) -> Iterator[list[str]]:
    """Yield the cells of every markdown table row, separator rows excluded."""
    for header, body in tables(doc):
        yield header
        yield from body


def backticked(text: str) -> list[str]:
    return BACKTICKED.findall(text)


def repo_paths(text: str) -> list[str]:
    """Backticked tokens that look like literal repository paths."""
    return [
        token
        for token in backticked(text)
        if token.startswith(REPO_PREFIXES)
        and not any(marker in token for marker in _NOT_A_LITERAL_PATH)
    ]


def is_uncheckable(token: str) -> bool:
    return token.startswith(UNCHECKABLE_ROOTS)


def path_missing(repo_root: Path, token: str) -> bool:
    """True only when the path is known to be absent — a DVC-only path is never `missing`."""
    if is_uncheckable(token):
        return False
    return not (repo_root / token.rstrip("/")).exists()


def broken_paths(repo_root: Path, doc: str) -> list[str]:
    """Every cited repository path that does not resolve."""
    return sorted({token for token in repo_paths(doc) if path_missing(repo_root, token)})


def _is_environment_absence_claim(claim: str) -> bool:
    lowered = claim.lower()
    if any(marker in lowered for marker in LOCKFILE_MARKERS):
        return True
    if "environment" not in lowered:
        return False
    return any(marker in lowered for marker in ABSENCE_MARKERS)


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a namespace stub, or a parent that will not import
        return False


def false_absence_claims(doc: str) -> list[tuple[str, str]]:
    """Claims that the environment lacks a package it can import."""
    offenders: list[tuple[str, str]] = []
    for claim in claims(doc):
        if not _is_environment_absence_claim(claim):
            continue
        for token in backticked(claim):
            if MODULE_NAME.match(token) and _importable(token):
                offenders.append((token, claim))
    return offenders


def stale_deferrals(repo_root: Path, doc: str) -> list[str]:
    """Claims that promise something for later without naming anything that is missing."""
    offenders: list[str] = []
    for claim in claims(doc):
        if not DEFERRAL.search(claim):
            continue
        if not any(path_missing(repo_root, token) for token in repo_paths(claim)):
            offenders.append(claim)
    return offenders
