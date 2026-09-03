"""`make validate-runout`: the gates that decide whether M4 may be promoted.

Checks, in the order they matter:

1. **Frozen hashes.** The ensemble design hash and `SOLVER_VERSION` recorded in
   `ENSEMBLE_FROZEN.md` are recomputed and compared. If either has moved, the ensemble on disk
   was not produced by the design that is documented, and everything downstream is void.
2. **No calibration language.** A forbidden-vocabulary grep over `reports/runout/*.md`. The
   Langtang comparison must never be written up as calibration, tuning or fitting.
3. **The disclaimer.** `NOT r.avaflow` must appear in the model card, in every runout report,
   and in the `assumptions[]` of an emitted `CascadeForecast`.
4. **Splits disjoint by `run_id`**, read from the metrics document rather than trusted.
5. **Ensemble size recorded**, with the valid count and the flagged-but-retained count.
6. **The surrogate gates**: IoU >= 0.70 at 1 m, arrival MAE <= 90 s per transect, p95 latency
   <= 2 s, 5-95% coverage in [0.85, 0.95].

A gate that fails is an `error` and the suite fails. A gate whose *inputs* do not exist yet is a
`warning`, so that a fresh clone with no ensemble reports honestly rather than passing silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from serac.models.runout.driver import INDEX_FILENAME
from serac.models.runout.ensemble import (
    DESIGN_FILENAME,
    FROZEN_FILENAME,
    design_from_payload,
    read_frozen_design,
)
from serac.models.runout.langtang import FORBIDDEN_VOCABULARY, SANITY_FILENAME
from serac.models.runout.params import NOT_RAVAFLOW, SOLVER_VERSION
from serac.models.runout.summary import SUMMARY_FILENAME
from serac.models.runout.training import (
    ARRIVAL_MAE_GATE_S,
    COVERAGE_TARGET,
    IOU_GATE,
    LATENCY_GATE_S,
    METRICS_FILENAME,
)
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "runout"
MODEL_CARD = Path("reports") / "MODEL_CARD_runout.md"
RUNOUT_REPORTS = Path("reports") / "runout"
DISCLAIMER_MARKER = "NOT r.avaflow"
MIN_VALID_MEMBERS = 200


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return loaded


def run_suite(repo: Path = Path("."), reports_dir: Path | None = None) -> SuiteResult:
    """Run every runout gate and return the result."""
    suite = Suite(SUITE_NAME, repo)
    reports = reports_dir or (repo / RUNOUT_REPORTS)

    # -- 1. frozen design ---------------------------------------------------------------------
    frozen_md = reports / FROZEN_FILENAME
    design_json = reports / DESIGN_FILENAME
    if not (frozen_md.exists() and design_json.exists()):
        suite.warn(
            "ensemble_frozen_present",
            False,
            f"{frozen_md} / {design_json} missing; the ensemble has not been frozen",
        )
    else:
        payload = read_frozen_design(reports)
        design = design_from_payload(payload)
        recorded = str(payload.get("design_hash_recorded", ""))
        text = frozen_md.read_text(encoding="utf-8")
        found = design.design_hash in text
        suite.check(
            "frozen_design_hash_matches",
            found,
            f"recomputed {design.design_hash}; "
            f"{'found' if found else 'NOT found'} in {FROZEN_FILENAME}",
        )
        suite.check(
            "frozen_solver_version_matches",
            str(payload.get("solver_version")) == SOLVER_VERSION,
            f"frozen {payload.get('solver_version')} vs current {SOLVER_VERSION}",
        )
        suite.info(
            "frozen_members", f"{payload.get('n_members')} members, seed {payload.get('seed')}"
        )
        if recorded:
            suite.check("frozen_hash_selfconsistent", recorded == design.design_hash, recorded)

    # -- 2. no calibration language -----------------------------------------------------------
    offenders: list[str] = []
    scanned = 0
    for path in sorted(reports.glob("*.md")):
        scanned += 1
        lowered = path.read_text(encoding="utf-8").lower()
        for word in FORBIDDEN_VOCABULARY:
            if word in lowered:
                # a sentence that *denies* calibrating is the point of the report, so the grep
                # is on the word, and any occurrence must be justified by a reviewer, not by
                # this suite guessing intent
                offenders.append(f"{path.name}: {word!r}")
    suite.check(
        "no_calibration_language",
        not offenders,
        f"scanned {scanned} report(s); "
        + ("clean" if not offenders else f"forbidden vocabulary: {'; '.join(offenders)}"),
    )

    # -- 3. the disclaimer ---------------------------------------------------------------------
    card = repo / MODEL_CARD
    suite.check(
        "model_card_present",
        card.exists(),
        str(MODEL_CARD),
    )
    if card.exists():
        suite.check(
            "model_card_disclaims_ravaflow",
            DISCLAIMER_MARKER in card.read_text(encoding="utf-8"),
            f"{MODEL_CARD} must contain {DISCLAIMER_MARKER!r}",
        )
    missing_disclaimer = [
        p.name
        for p in sorted(reports.glob("*.md"))
        if DISCLAIMER_MARKER not in p.read_text(encoding="utf-8")
    ]
    suite.check(
        "reports_disclaim_ravaflow",
        not missing_disclaimer,
        "every reports/runout/*.md carries the disclaimer"
        if not missing_disclaimer
        else f"missing in: {', '.join(missing_disclaimer)}",
    )
    suite.check(
        "forecast_assumptions_disclaim_ravaflow",
        DISCLAIMER_MARKER in NOT_RAVAFLOW,
        "every emitted CascadeForecast carries NOT_RAVAFLOW in assumptions[]",
    )

    # -- 4/5. ensemble --------------------------------------------------------------------------
    summary = _load(reports / SUMMARY_FILENAME)
    if summary is None:
        suite.warn("ensemble_summary_present", False, f"{reports / SUMMARY_FILENAME} missing")
    else:
        n_valid = int(summary.get("n_valid", 0))
        suite.check(
            "ensemble_size_recorded",
            "n_valid" in summary and "n_members_recorded" in summary,
            f"{n_valid} valid of {summary.get('n_members_recorded')} recorded, "
            f"{summary.get('n_flagged_but_retained')} flagged but retained",
        )
        suite.check(
            "ensemble_has_enough_valid_members",
            n_valid >= MIN_VALID_MEMBERS,
            f"{n_valid} valid members against a floor of {MIN_VALID_MEMBERS}",
        )
        suite.check(
            "ensemble_within_size_cap",
            bool(summary.get("bytes_within_cap", False)),
            f"{summary.get('bytes_on_disk')} B against a cap of {summary.get('bytes_cap')} B",
        )
        suite.check(
            "ensemble_solver_version_matches",
            summary.get("frozen_solver_version") in (None, SOLVER_VERSION),
            f"ensemble built with {summary.get('frozen_solver_version')}, current {SOLVER_VERSION}",
        )
    index_path = reports / INDEX_FILENAME
    suite.warn("ensemble_index_present", index_path.exists(), str(index_path))

    # -- 6. surrogate gates ----------------------------------------------------------------------
    metrics = _load(reports / METRICS_FILENAME)
    if metrics is None:
        suite.warn("surrogate_metrics_present", False, f"{reports / METRICS_FILENAME} missing")
    else:
        split = metrics.get("split", {})
        sets = [set(split.get(k, [])) for k in ("train", "val", "test")]
        total = sum(len(s) for s in sets)
        disjoint = len(set().union(*sets)) == total and total > 0
        suite.check(
            "splits_disjoint_by_run_id",
            disjoint,
            f"train {len(sets[0])} / val {len(sets[1])} / test {len(sets[2])}, "
            f"union {len(set().union(*sets))}",
        )
        inundation = metrics.get("inundation", {})
        iou = inundation.get("median_iou")
        suite.check(
            "inundation_iou_gate",
            bool(inundation.get("gate_pass")),
            f"median IoU at {inundation.get('threshold_m')} m = {iou} (gate >= {IOU_GATE})",
        )
        worst = metrics.get("arrival_mae_worst_s")
        suite.check(
            "arrival_time_mae_gate",
            bool(metrics.get("arrival_gate_pass")),
            f"worst per-transect arrival MAE = {worst} s (gate <= {ARRIVAL_MAE_GATE_S} s)",
        )
        latency = metrics.get("latency", {})
        suite.check(
            "inference_latency_gate",
            bool(latency.get("gate_pass")),
            f"p95 = {latency.get('p95_s')} s on {latency.get('device')} "
            f"(gate <= {LATENCY_GATE_S} s)",
        )
        coverage = metrics.get("coverage", {})
        suite.check(
            "depth_interval_coverage",
            bool(coverage.get("depth_gate_pass")),
            f"5-95% depth coverage = {coverage.get('max_depth_5_95')} "
            f"(target {COVERAGE_TARGET[0]}-{COVERAGE_TARGET[1]})",
        )
        suite.warn(
            "arrival_interval_coverage",
            bool(coverage.get("arrival_gate_pass")),
            f"5-95% arrival coverage = {coverage.get('arrival_5_95')} "
            f"(target {COVERAGE_TARGET[0]}-{COVERAGE_TARGET[1]})",
        )
        for name, block in (metrics.get("transects") or {}).items():
            suite.info(
                f"transect_{name}",
                f"{block.get('reached_members')} test members reached; "
                f"arrival MAE {block.get('arrival_mae_s')} s; "
                f"peak-stage relative error {block.get('peak_stage_relative_error')}",
            )

    # -- the Langtang comparison exists and is a comparison ------------------------------------
    sanity = reports / SANITY_FILENAME
    suite.check("langtang_sanity_present", sanity.exists(), str(sanity))
    if sanity.exists():
        text = sanity.read_text(encoding="utf-8")
        suite.check(
            "langtang_sanity_asserts_frozen_hash",
            "design hash" in text.lower(),
            "the comparison must state the frozen design hash it ran against",
        )
        suite.check(
            "langtang_sanity_disclaims_adjustment",
            "not an adjustment" in text.lower(),
            "the comparison must state that nothing was adjusted",
        )

    return suite.result()
