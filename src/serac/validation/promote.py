"""`serac validate stamp` and `serac promote`.

`stamp` collects the per-suite reports written by `serac validate <suite>` and records one
`reports/validation/latest.json` with the git sha and tree state they were produced on.
`promote` refuses unless that stamp exists, passed, was produced at the current HEAD on a
clean tree, and covers every required suite. It writes a promotion record and tags nothing;
humans act on the record.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel

from serac import __version__
from serac.validation.result import SuiteResult, git_sha, load_report

REQUIRED_SUITES: tuple[str, ...] = (
    "events",
    "aoi",
    "ingest",
    "cube",
    "stream",
    "contracts",
    "lfh",
    "runout",
    "watch",
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
    promoted_at: AwareDatetime
    git_sha: str
    serac_version: str = __version__
    stamp_path: str
    suites: dict[str, str]


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


def promotion_blockers(repo: Path, stamp: Stamp | None) -> list[str]:
    """Reasons `promote` must refuse; empty means promotable."""
    if stamp is None:
        return ["no validation stamp: run `make validate-serac` first"]
    blockers: list[str] = []
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
    return blockers


def promote(repo: Path, reports_dir: Path, promotions_dir: Path) -> PromotionRecord:
    stamp = load_stamp(reports_dir)
    blockers = promotion_blockers(repo, stamp)
    if blockers or stamp is None or stamp.git_sha is None:
        raise PromotionRefusedError(blockers or ["no stamp"])
    record = PromotionRecord(
        promoted_at=datetime.now(tz=UTC),
        git_sha=stamp.git_sha,
        stamp_path=str(reports_dir / "latest.json"),
        suites=stamp.suites,
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
