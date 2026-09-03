"""`make validate-watch` — the gate that decides whether M3 may be believed at all.

The checks are mostly about discipline rather than performance, because at n = 1 positive
event there is no performance number worth gating on. What can be gated on is whether the
result was obtained honestly:

1. The Chamoli backtest exists.
2. The pre-registration **precedes it by git ancestry**, and was not modified afterwards. This
   is the anti-hindsight check and it is done against git, not against a timestamp in a file
   that anyone could write.
3. The Langtang result is written, positive or null.
4. No schema and no report contains a failure-date or calibrated-probability field.
5. Insufficient data is honoured: units that could not be measured are reported as
   `insufficient_data`, never as `quiet`.
6. The causality test exists and passed.
7. The model card carries the "not a time-of-failure predictor" disclaimer.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from serac.validation.result import Severity, Suite, SuiteResult

PREREGISTRATION_PATH = "reports/watch/PREREGISTRATION.md"
BACKTEST_JSON = "reports/watch/backtest_chamoli.json"
BACKTEST_MD = "reports/watch/backtest_chamoli.md"
LANGTANG_MD = "reports/watch/backtest_langtang.md"
MODEL_CARD = "reports/MODEL_CARD_watch.md"
CAUSALITY_TEST = "tests/unit/watch/test_anomaly.py"
CAUSALITY_TEST_NAME = "test_appending_future_samples_then_truncating_leaves_scores_identical"

FORBIDDEN_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfailure_date\b"),
    re.compile(r"\bfailure_time_predicted\b"),
    re.compile(r"\bpredicted_failure\w*\b"),
    re.compile(r"\btime_to_failure\b"),
    re.compile(r"\bdays_to_failure\b"),
    re.compile(r"\bfailure_probability\b"),
    re.compile(r"\bprobability_of_failure\b"),
    re.compile(r"\bp_failure\b"),
    re.compile(r"\bexpected_failure\w*\b"),
)
"""Field and phrase names that would imply a predicted date or a calibrated probability.

`failure_time_utc` is deliberately **not** here: the backtest records the observed time of a
past event, which is a fact, not a prediction. The patterns above are the ones that would
describe a serac output.
"""

SCANNED_GLOBS = ("reports/watch/*.json", "reports/watch/*.md", "contracts/*.json")


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        return 1, str(exc)
    return out.returncode, out.stdout.strip()


def _first_commit_touching(repo: Path, path: str) -> str | None:
    """The oldest commit that touched `path`, or None when git does not know the file."""
    code, out = _git(repo, "log", "--reverse", "--format=%H", "--", path)
    if code != 0 or not out:
        return None
    return out.splitlines()[0]


def _commits_touching(repo: Path, path: str) -> list[str]:
    code, out = _git(repo, "log", "--format=%H", "--", path)
    return [] if code != 0 or not out else out.splitlines()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    code, _ = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return code == 0


def check_preregistration_precedes_backtest(suite: Suite, repo: Path) -> None:
    """The anti-hindsight check, done against git history rather than a self-reported date."""
    prereg_commit = _first_commit_touching(repo, PREREGISTRATION_PATH)
    backtest_commit = _first_commit_touching(repo, BACKTEST_JSON)
    if prereg_commit is None:
        suite.check(
            "preregistration_committed",
            False,
            f"{PREREGISTRATION_PATH} has no commit in git history",
        )
        return
    suite.check("preregistration_committed", True, f"introduced in {prereg_commit[:12]}")
    if backtest_commit is None:
        suite.check(
            "preregistration_precedes_backtest",
            False,
            f"{BACKTEST_JSON} has no commit in git history, so ancestry cannot be established",
        )
        return
    ordered = prereg_commit != backtest_commit and _is_ancestor(
        repo, prereg_commit, backtest_commit
    )
    suite.check(
        "preregistration_precedes_backtest",
        ordered,
        f"pre-registration {prereg_commit[:12]} "
        f"{'is' if ordered else 'is NOT'} an ancestor of backtest {backtest_commit[:12]}",
    )
    later = [c for c in _commits_touching(repo, PREREGISTRATION_PATH) if c != prereg_commit]
    unmodified = not later
    suite.check(
        "preregistration_unmodified_after_commit",
        unmodified,
        "never modified after its introducing commit"
        if unmodified
        else f"modified in {len(later)} later commit(s): {', '.join(c[:12] for c in later)}",
    )


def check_no_failure_date_anywhere(suite: Suite, repo: Path) -> None:
    """No schema and no report may carry a field implying a predicted date or probability."""
    offenders: list[str] = []
    scanned = 0
    for pattern in SCANNED_GLOBS:
        for path in sorted(repo.glob(pattern)):
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in FORBIDDEN_FIELD_PATTERNS:
                for match in needle.finditer(text):
                    offenders.append(f"{path.relative_to(repo)}: {match.group(0)}")
    suite.check(
        "no_failure_date_field_in_schemas_or_reports",
        not offenders,
        f"scanned {scanned} file(s); "
        + (
            "none carry a predicted-date or calibrated-probability field"
            if not offenders
            else "offenders: " + "; ".join(offenders[:10])
        ),
    )


def check_backtest(suite: Suite, repo: Path) -> dict[str, Any] | None:
    """The Chamoli backtest exists, is complete, and reports the false-alarm burden."""
    path = repo / BACKTEST_JSON
    if not path.exists():
        suite.check("chamoli_backtest_exists", False, f"{BACKTEST_JSON} is missing")
        return None
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    suite.check(
        "chamoli_backtest_exists",
        True,
        f"{summary.get('n_steps')} monthly steps over {summary.get('n_units_total')} slope units",
    )
    suite.check(
        "chamoli_backtest_reports_false_alarm_burden",
        "concurrent_other_watch_units_at_first_watch" in summary
        and "median_watch_units_per_step" in summary,
        "the concurrent watch-tier count is present"
        if "median_watch_units_per_step" in summary
        else "the false-alarm burden is not reported",
    )
    suite.check(
        "chamoli_backtest_has_a_markdown_writeup",
        (repo / BACKTEST_MD).exists(),
        BACKTEST_MD,
    )
    suite.check(
        "chamoli_backtest_declares_the_tier_is_not_a_probability",
        "not a calibrated failure probability" in str(summary.get("disclaimer", "")),
        str(summary.get("disclaimer", ""))[:160],
    )
    lead = summary.get("lead_time_days_to_first_watch")
    suite.info(
        "chamoli_result",
        f"reached watch: {summary.get('reached_watch')}; lead time to first watch: {lead} d; "
        f"other units at watch on that step: "
        f"{summary.get('concurrent_other_watch_units_at_first_watch')}",
    )
    return payload


def check_insufficient_data_honoured(suite: Suite, payload: dict[str, Any] | None) -> None:
    """A unit that could not be measured must be `insufficient_data`, never `quiet`."""
    from serac.models.watch.anomaly import Tier

    if payload is None:
        suite.check("insufficient_data_honoured", False, "no backtest to inspect")
        return
    steps = payload.get("steps", [])
    if not steps:
        suite.check("insufficient_data_honoured", False, "the backtest recorded no steps")
        return
    bad = [
        s
        for s in steps
        if s.get("target_reason") and s.get("target_tier") != Tier.insufficient_data.value
    ]
    suite.check(
        "insufficient_data_honoured",
        not bad,
        "every step carrying an insufficiency reason is tiered insufficient_data"
        if not bad
        else f"{len(bad)} step(s) report a reason but a measurable tier",
    )
    counts = payload.get("summary", {}).get("steps_by_target_tier", {})
    suite.info("target_tier_histogram", json.dumps(counts, sort_keys=True))


def check_langtang(suite: Suite, repo: Path) -> None:
    """The Langtang write-up exists and separates observability from the absence of a precursor."""
    path = repo / LANGTANG_MD
    if not path.exists():
        suite.check("langtang_result_written", False, f"{LANGTANG_MD} is missing")
        return
    text = path.read_text(encoding="utf-8")
    suite.check("langtang_result_written", True, f"{len(text)} characters")
    suite.check(
        "langtang_separates_observability_from_absence_of_precursor",
        "we could not have seen it" in text.lower() and "no precursor" in text.lower(),
        "both named sections are present"
        if "we could not have seen it" in text.lower()
        else "the write-up does not name both cases",
    )


def check_causality_test(suite: Suite, repo: Path) -> None:
    """The causality test exists in the tree; `make test` is what proves it passes."""
    path = repo / CAUSALITY_TEST
    present = path.exists() and CAUSALITY_TEST_NAME in path.read_text(encoding="utf-8")
    suite.check(
        "causality_test_recorded",
        present,
        f"{CAUSALITY_TEST}::{CAUSALITY_TEST_NAME}"
        if present
        else f"{CAUSALITY_TEST_NAME} not found in {CAUSALITY_TEST}",
    )
    hindsight = repo / "tests" / "unit" / "watch" / "test_no_hindsight.py"
    suite.check(
        "no_hindsight_test_recorded",
        hindsight.exists(),
        "tests/unit/watch/test_no_hindsight.py",
    )


def check_model_card(suite: Suite, repo: Path) -> None:
    path = repo / MODEL_CARD
    if not path.exists():
        suite.check("model_card_exists", False, f"{MODEL_CARD} is missing")
        return
    text = path.read_text(encoding="utf-8").lower()
    suite.check("model_card_exists", True, MODEL_CARD)
    suite.check(
        "model_card_disclaimer_present",
        "not a time-of-failure predictor" in text,
        "'not a time-of-failure predictor' is present"
        if "not a time-of-failure predictor" in text
        else "the required out-of-scope disclaimer is missing",
    )
    for topic, needle in (
        ("c_band_decorrelation", "decorrelation"),
        ("layover_shadow", "layover"),
        ("brittle_failure", "brittle"),
        ("monsoon_cloud", "monsoon"),
    ):
        suite.warn(
            f"model_card_documents_{topic}",
            needle in text,
            f"'{needle}' {'appears' if needle in text else 'does not appear'} in the model card",
        )


def check_provenance(suite: Suite, repo: Path) -> None:
    """Transient ledger rows are surfaced as a named warning, not silently accepted."""
    from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
    from serac.domain.manifest import Retention

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    transient = [e for e in ledger.entries() if e.retention is Retention.transient]
    suite.warn(
        "transient_ledger_rows_present",
        not transient,
        f"{len(transient)} row(s) were hashed on arrival then deleted and can never be "
        "re-hashed (HyP3 product zips, cropped to the AOI); the crops that replaced them are "
        "ordinary retained rows"
        if transient
        else "no transient rows",
    )
    selections = sorted((repo / "reports" / "watch").glob("track_selection_*.json"))
    suite.check(
        "track_selection_recorded",
        bool(selections),
        ", ".join(p.name for p in selections) if selections else "no track selection report",
    )
    for path in selections:
        payload = json.loads(path.read_text(encoding="utf-8"))
        suite.info(
            f"track_selection_{payload.get('aoi_id')}",
            f"path {payload.get('selected_path')}; rule sha256 "
            f"{str(payload.get('rule_sha256'))[:12]}; {payload.get('selected_reason')}",
        )


def run_suite(repo: Path | None = None) -> SuiteResult:
    """Run every M3 gate. `repo` defaults to the current working directory."""
    root = (repo or Path.cwd()).resolve()
    suite = Suite("watch", root)
    check_preregistration_precedes_backtest(suite, root)
    payload = check_backtest(suite, root)
    check_insufficient_data_honoured(suite, payload)
    check_langtang(suite, root)
    check_no_failure_date_anywhere(suite, root)
    check_causality_test(suite, root)
    check_model_card(suite, root)
    check_provenance(suite, root)
    return suite.result()


__all__ = ["Severity", "run_suite"]
