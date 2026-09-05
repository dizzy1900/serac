"""`serac validate stamp` and `serac promote`.

`stamp` collects the per-suite reports written by `serac validate <suite>` and records one
`reports/validation/latest.json` with the git sha and tree state they were produced on.
`promote` refuses unless that stamp exists, passed, was produced at the current HEAD on a
clean tree, covers every required suite, and a named human approved this tree through
`PROMOTE_APPROVED_BY`. It writes a promotion record and tags nothing; humans act on the
record.

The approval is a parameter, not an environment read hidden in here: the CLI resolves
`PROMOTE_APPROVED_BY` at its boundary and passes the value down. `PromotionRecord.approved_by`
is a required field, so a record that names nobody cannot be constructed at all — no future
promotion path can forget the human and still write something to `reports/promotion/`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, Field

from serac import __version__
from serac.validation.result import SuiteResult, git_sha, load_report

APPROVAL_ENV_VAR = "PROMOTE_APPROVED_BY"

# Values that attest to nothing. Promotion is a person's decision, and the record has to say
# whose, so that someone can be asked afterwards why this tree was promoted. A boolean-ish
# flag or a job name would let a script rubber-stamp itself, which is the failure this gate
# exists to prevent; these are refused rather than recorded as an approver.
_NON_NAMES: frozenset[str] = frozenset(
    {
        "-",
        "anon",
        "anonymous",
        "approve",
        "approved",
        "auto",
        "automated",
        "bot",
        "ci",
        "false",
        "n",
        "n/a",
        "na",
        "nil",
        "no",
        "none",
        "null",
        "ok",
        "okay",
        "robot",
        "someone",
        "true",
        "unknown",
        "y",
        "yes",
        "0",
        "1",
    }
)

REQUIRED_SUITES: tuple[str, ...] = (
    "events",
    "aoi",
    "ingest",
    "cube",
    "stream",
    "contracts",
    "lfh",
    "discriminator",
    "runout",
    "watch",
    "e2e",
)


class Stamp(BaseModel):
    """What `validate-serac` proved, and on which tree."""

    contract_version: str = "0.1.0"
    stamped_at: AwareDatetime
    serac_version: str = __version__
    git_sha: str | None
    tree_clean: bool | None
    suites: dict[str, str]  # suite -> "passed" | "failed"
    missing: list[str]
    passed: bool


class PromotionRecord(BaseModel):
    """Who promoted which tree, and what the gates had proved about it.

    `approved_by` has no default: the human is part of the record's identity, not an optional
    annotation on it.
    """

    promoted_at: AwareDatetime
    git_sha: str
    serac_version: str = __version__
    stamp_path: str
    suites: dict[str, str]
    approved_by: str = Field(min_length=2)


def tree_is_clean(repo: Path) -> bool | None:
    """True when `git status --porcelain` is empty; None outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Validation and replay reports are gitignored, so they never dirty the tree.
    return out.stdout.strip() == ""


def make_stamp(repo: Path, reports_dir: Path, required: tuple[str, ...] = REQUIRED_SUITES) -> Stamp:
    suites: dict[str, str] = {}
    missing: list[str] = []
    for name in required:
        path = reports_dir / f"{name}.json"
        if not path.exists():
            missing.append(name)
            continue
        result: SuiteResult = load_report(path)
        suites[name] = result.status
    passed = not missing and all(v == "passed" for v in suites.values())
    return Stamp(
        stamped_at=datetime.now(tz=UTC),
        git_sha=git_sha(repo),
        tree_clean=tree_is_clean(repo),
        suites=suites,
        missing=missing,
        passed=passed,
    )


def write_stamp(stamp: Stamp, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "latest.json"
    path.write_text(stamp.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_stamp(reports_dir: Path) -> Stamp | None:
    path = reports_dir / "latest.json"
    if not path.exists():
        return None
    return Stamp.model_validate(json.loads(path.read_text(encoding="utf-8")))


def approver_name(approved_by: str | None) -> str | None:
    """The approver as it should be recorded, or None when the value names no human.

    Whitespace-only, absent, and placeholder values (`1`, `yes`, `ci`, …) all return None.
    """
    name = (approved_by or "").strip()
    if len(name) < 2 or name.casefold() in _NON_NAMES:
        return None
    return name


def approval_blocker(approved_by: str | None) -> str | None:
    """The reason this approval is not a human attestation, or None when it is one."""
    if approver_name(approved_by) is not None:
        return None
    given = (approved_by or "").strip()
    if not given:
        return (
            f"not approved by a human: set {APPROVAL_ENV_VAR} to the name of the person "
            f"approving this promotion, e.g. `{APPROVAL_ENV_VAR}='A. Name' make promote`"
        )
    return (
        f"{APPROVAL_ENV_VAR}={given!r} names no one: promotion is a person's decision and "
        "the record has to say whose"
    )


def promotion_blockers(repo: Path, stamp: Stamp | None, approved_by: str | None) -> list[str]:
    """Reasons `promote` must refuse; empty means promotable.

    `approved_by` is required rather than defaulted: an unapproved promotion and a promotion
    whose caller forgot to ask about approval must not look the same to this function.
    All blockers are collected, so one refusal shows everything that has to be fixed.
    """
    blockers: list[str] = []
    if stamp is None:
        blockers.append("no validation stamp: run `make validate-serac` first")
    else:
        if stamp.missing:
            blockers.append(f"suites not run: {', '.join(stamp.missing)}")
        failed = sorted(k for k, v in stamp.suites.items() if v != "passed")
        if failed:
            blockers.append(f"suites failed: {', '.join(failed)}")
        head = git_sha(repo)
        if head is None or stamp.git_sha is None:
            blockers.append("not a git checkout; promotion requires a committed tree")
        elif head != stamp.git_sha:
            blockers.append(f"stamp is for {stamp.git_sha[:12]} but HEAD is {head[:12]}")
        if stamp.tree_clean is not True or tree_is_clean(repo) is not True:
            blockers.append("working tree is not clean")
    approval = approval_blocker(approved_by)
    if approval is not None:
        blockers.append(approval)
    return blockers


def promote(
    repo: Path, reports_dir: Path, promotions_dir: Path, approved_by: str | None
) -> PromotionRecord:
    stamp = load_stamp(reports_dir)
    blockers = promotion_blockers(repo, stamp, approved_by)
    approver = approver_name(approved_by)
    if blockers or stamp is None or stamp.git_sha is None or approver is None:
        raise PromotionRefusedError(blockers or ["no stamp"])
    record = PromotionRecord(
        promoted_at=datetime.now(tz=UTC),
        git_sha=stamp.git_sha,
        stamp_path=str(reports_dir / "latest.json"),
        suites=stamp.suites,
        approved_by=approver,
    )
    promotions_dir.mkdir(parents=True, exist_ok=True)
    (promotions_dir / f"{stamp.git_sha}.json").write_text(
        record.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return record


class PromotionRefusedError(Exception):
    def __init__(self, blockers: list[str]) -> None:
        super().__init__("; ".join(blockers))
        self.blockers = blockers
