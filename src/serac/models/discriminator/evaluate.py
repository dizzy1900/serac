"""Evaluation, bootstrap intervals and the anti-tuning seal.

**Bootstrap over event groups, not windows.** A group contributes one positive and up to six
matched windows cut at the same stations, so resampling windows would treat six views of one
event as six independent observations and shrink every interval by roughly the square root of
that. The intervals here are wide, and they are wide honestly: with ten held-out positives, a
95% interval on F1 that spans half the unit interval is the correct answer, not a failure of
the method.

**The seal.** `reports/m1/seal.json` records the hash of every configuration constant the
moment the test set is first scored. `check_seal` refuses a later test evaluation whose config
hash differs. The failure mode it exists to stop is the quiet one: score on test, see 0.61,
adjust `num_leaves`, score again, report 0.74 as if it had been a single shot. With a seal that
sequence has to be a deliberate act — bumping the seal version and saying so in the report —
rather than an accident of iteration.

**What is not claimed.** ROC-AUC and F1 on ten positives are point estimates with intervals
that contain a great deal. Per-region confusion matrices for regions with one positive are
printed because hiding them would be worse, not because they mean anything. Every table in the
model card carries its denominator for that reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator import baseline as bl
from serac.models.discriminator import catalog as cat
from serac.models.discriminator import windows as win
from serac.models.discriminator.dataset import (
    LORO_VAL_FRACTION,
    TIME_FORWARD_TRAIN_BEFORE,
    TIME_FORWARD_VAL_THROUGH,
)
from serac.models.discriminator.features import FEATURE_NAMES

EVALUATE_VERSION = "0.1.0"

SEAL_PATH: Final = Path("reports/m1/seal.json")
BOOTSTRAP_ITERATIONS: Final = 2000
BOOTSTRAP_SEED: Final = 20260903
RELIABILITY_BINS: Final = 10


class EvaluationError(SeracError):
    """An evaluation could not be run, or the seal refused it."""


class SealBrokenError(EvaluationError):
    """The configuration changed between test evaluations."""


# --- the seal -----------------------------------------------------------------------------


def config_fingerprint() -> dict[str, Any]:
    """Every constant that could be tuned toward a test score, in one dict."""
    return {
        "seal_version": 1,
        "features": {"names": list(FEATURE_NAMES), "version": "0.1.0"},
        "windows": {
            "min_distance_km": win.MIN_DISTANCE_KM,
            "max_distance_km": win.MAX_DISTANCE_KM,
            "max_stations": win.MAX_STATIONS_PER_EVENT,
            "azimuth_bins": win.AZIMUTH_BINS,
            "sampling_rate_hz": win.TARGET_SAMPLING_RATE_HZ,
            "bandpass_hz": list(win.BANDPASS_HZ),
            "pre_filt": list(win.PRE_FILT),
            "min_valid_fraction": win.MIN_VALID_FRACTION,
            "min_stations_per_window": win.MIN_STATIONS_PER_WINDOW,
        },
        "catalog": {
            "dedupe_seconds": cat.DEDUPE_SECONDS,
            "dedupe_km": cat.DEDUPE_KM,
            "negatives_per_positive": cat.NEGATIVES_PER_POSITIVE,
            "negative_max_distance_km": cat.NEGATIVE_MAX_DISTANCE_KM,
            "negative_epoch_years": cat.NEGATIVE_EPOCH_YEARS,
            "negative_magnitude_band": [
                cat.NEGATIVE_MIN_MAGNITUDE,
                cat.NEGATIVE_MAX_MAGNITUDE,
            ],
            "noise_offset_s": cat.NOISE_OFFSET_S,
            "window_pre_origin_s": cat.WINDOW_PRE_ORIGIN_S,
            "window_length_s": cat.WINDOW_LENGTH_S,
            "forced_test_groups": sorted(cat.FORCED_TEST_GROUPS),
        },
        "splits": {
            "time_forward_train_before": TIME_FORWARD_TRAIN_BEFORE,
            "time_forward_val_through": TIME_FORWARD_VAL_THROUGH,
            "loro_val_fraction": LORO_VAL_FRACTION,
        },
        "baseline": {"params": dict(bl.LGBM_PARAMS), "rounds": bl.NUM_BOOST_ROUND},
        "bootstrap": {"iterations": BOOTSTRAP_ITERATIONS, "seed": BOOTSTRAP_SEED},
    }


def config_hash() -> str:
    payload = json.dumps(config_fingerprint(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Seal(BaseModel):
    """The configuration the test set was first scored under."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sealed_at_utc: AwareDatetime
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: dict[str, Any]
    schemes_evaluated: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def check_seal(repo: Path, scheme: str) -> Seal:
    """Seal the config on the first test evaluation; refuse a later one under a changed config."""
    path = repo / SEAL_PATH
    current = config_hash()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        seal = Seal(
            sealed_at_utc=datetime.now(tz=UTC),
            config_sha256=current,
            config=config_fingerprint(),
            schemes_evaluated=[scheme],
            notes=[
                "Sealed at the first test evaluation. Any later test evaluation under a "
                "different configuration is refused: this is what makes 'evaluated once per "
                "configuration' a mechanical fact rather than an intention.",
                "To evaluate a genuinely new configuration, bump `seal_version` in "
                "`config_fingerprint`, delete this file, and say in the report that the test "
                "set was scored more than once and why.",
            ],
        )
        path.write_text(seal.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return seal

    seal = Seal.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if seal.config_sha256 != current:
        changed = [
            key for key in config_fingerprint() if seal.config.get(key) != config_fingerprint()[key]
        ]
        raise SealBrokenError(
            f"the configuration changed since the test set was first scored on "
            f"{seal.sealed_at_utc.isoformat()}. Changed sections: {changed}. Test evaluation is "
            "refused. See reports/m1/seal.json."
        )
    if scheme not in seal.schemes_evaluated:
        updated = seal.model_copy(update={"schemes_evaluated": [*seal.schemes_evaluated, scheme]})
        path.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return updated
    return seal


# --- metrics ------------------------------------------------------------------------------


class ClassMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    support: int = Field(ge=0)
    precision: float
    recall: float
    f1: float


class Interval(BaseModel):
    """A point estimate with a percentile bootstrap interval over test event groups."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point: float
    low: float
    high: float
    level: float = 0.95
    n_resamples: int = Field(ge=0)
    resample_unit: str = "event_group"


class Reliability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brier: float
    ece: float
    bin_edges: list[float]
    bin_counts: list[int]
    bin_mean_probability: list[float]
    bin_observed_frequency: list[float]


class EvaluationResult(BaseModel):
    """One scheme's test-set result. Produced once, under the seal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluate_version: str = EVALUATE_VERSION
    scheme: str
    model_name: str
    evaluated_at_utc: AwareDatetime
    config_sha256: str
    n_test_windows: int = Field(ge=0)
    n_test_groups: int = Field(ge=0)
    n_test_positives: int = Field(ge=0)
    per_class: list[ClassMetrics]
    macro_f1: Interval
    mass_movement_f1: Interval
    mass_movement_precision: Interval
    mass_movement_recall: Interval
    roc_auc: Interval
    confusion: list[list[int]]
    confusion_by_region: dict[str, list[list[int]]]
    reliability: Reliability
    forced_group_outcomes: dict[str, dict[str, Any]]
    notes: list[str] = Field(default_factory=list)


def _prf(truth: np.ndarray, predicted: np.ndarray, index: int) -> tuple[float, float, float]:
    tp = float(np.sum((predicted == index) & (truth == index)))
    fp = float(np.sum((predicted == index) & (truth != index)))
    fn = float(np.sum((predicted != index) & (truth == index)))
    precision = tp / (tp + fp) if tp + fp > 0 else 0.0
    recall = tp / (tp + fn) if tp + fn > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return precision, recall, f1


def roc_auc(truth_binary: np.ndarray, scores: np.ndarray) -> float:
    """Rank-based one-vs-rest AUC; 0.5 when a class is absent (and reported as such)."""
    positives = truth_binary.astype(bool)
    n_pos, n_neg = int(positives.sum()), int((~positives).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(scores.size, dtype=np.float64)
    ranks[order] = np.arange(1, scores.size + 1, dtype=np.float64)
    # Average ranks within ties so a model that outputs one constant scores 0.5, not 1.0.
    sorted_scores = scores[order]
    start = 0
    for end in range(1, scores.size + 1):
        if end == scores.size or sorted_scores[end] != sorted_scores[start]:
            ranks[order[start:end]] = ranks[order[start:end]].mean()
            start = end
    return float((ranks[positives].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _bootstrap(
    groups: np.ndarray,
    statistic: Any,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """Percentile interval of `statistic(indices)` resampling **groups** with replacement."""
    unique = np.unique(groups)
    by_group = {g: np.flatnonzero(groups == g) for g in unique}
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(iterations):
        drawn = rng.choice(unique, size=unique.size, replace=True)
        indices = np.concatenate([by_group[g] for g in drawn])
        value = statistic(indices)
        if value is not None and np.isfinite(value):
            values.append(value)
    if not values:
        return float("nan"), float("nan")
    array = np.asarray(values)
    return float(np.percentile(array, 2.5)), float(np.percentile(array, 97.5))


def _interval(
    point: float, groups: np.ndarray, statistic: Any, iterations: int = BOOTSTRAP_ITERATIONS
) -> Interval:
    low, high = _bootstrap(groups, statistic, iterations=iterations)
    return Interval(point=point, low=low, high=high, n_resamples=iterations)


def reliability(probabilities: np.ndarray, truth_binary: np.ndarray) -> Reliability:
    """Brier score, expected calibration error and the reliability bins behind them."""
    p = np.clip(np.asarray(probabilities, dtype=np.float64), 0.0, 1.0)
    y = np.asarray(truth_binary, dtype=np.float64)
    edges = np.linspace(0.0, 1.0, RELIABILITY_BINS + 1)
    counts, mean_p, observed = [], [], []
    ece = 0.0
    for lower, upper in pairwise(edges):
        mask = (p >= lower) & (p < upper if upper < 1.0 else p <= upper)
        n = int(mask.sum())
        counts.append(n)
        if n == 0:
            mean_p.append(0.0)
            observed.append(0.0)
            continue
        bin_p, bin_y = float(p[mask].mean()), float(y[mask].mean())
        mean_p.append(bin_p)
        observed.append(bin_y)
        ece += (n / p.size) * abs(bin_p - bin_y)
    return Reliability(
        brier=float(np.mean((p - y) ** 2)),
        ece=float(ece),
        bin_edges=[float(e) for e in edges],
        bin_counts=counts,
        bin_mean_probability=mean_p,
        bin_observed_frequency=observed,
    )


def confusion_matrix(truth: np.ndarray, predicted: np.ndarray) -> list[list[int]]:
    n = len(bl.CLASSES)
    matrix = np.zeros((n, n), dtype=int)
    for actual, guess in zip(truth, predicted, strict=True):
        matrix[int(actual), int(guess)] += 1
    return matrix.tolist()


def evaluate(
    *,
    scheme: str,
    model_name: str,
    truth: np.ndarray,
    predicted: np.ndarray,
    probabilities: np.ndarray,
    groups: np.ndarray,
    regions: np.ndarray,
    group_ids: Sequence[str],
    repo: Path,
    forced_groups: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> EvaluationResult:
    """Score the held-out fold once, under the seal, with group-level bootstrap intervals."""
    seal = check_seal(repo, scheme)
    truth = np.asarray(truth, dtype=int)
    predicted = np.asarray(predicted, dtype=int)
    probabilities = np.asarray(probabilities, dtype=np.float64)
    binary = (truth == bl.POSITIVE_CLASS_INDEX).astype(int)

    per_class = []
    for index, label in enumerate(bl.CLASSES):
        precision, recall, f1 = _prf(truth, predicted, index)
        per_class.append(
            ClassMetrics(
                label=label,
                support=int((truth == index).sum()),
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )
    macro = float(np.mean([m.f1 for m in per_class]))
    mm_precision, mm_recall, mm_f1 = _prf(truth, predicted, bl.POSITIVE_CLASS_INDEX)

    def stat_macro(idx: np.ndarray) -> float:
        return float(
            np.mean([_prf(truth[idx], predicted[idx], i)[2] for i in range(len(bl.CLASSES))])
        )

    def stat_mm(component: int) -> Any:
        def inner(idx: np.ndarray) -> float:
            return _prf(truth[idx], predicted[idx], bl.POSITIVE_CLASS_INDEX)[component]

        return inner

    def stat_auc(idx: np.ndarray) -> float:
        return roc_auc(binary[idx], probabilities[idx])

    by_region: dict[str, list[list[int]]] = {}
    for region in sorted(set(regions.tolist())):
        mask = regions == region
        by_region[str(region)] = confusion_matrix(truth[mask], predicted[mask])

    outcomes: dict[str, dict[str, Any]] = {}
    for forced in forced_groups:
        mask = groups == forced
        if not mask.any():
            outcomes[forced] = {"present_in_test": False}
            continue
        positive = mask & (truth == bl.POSITIVE_CLASS_INDEX)
        outcomes[forced] = {
            "present_in_test": True,
            "n_windows": int(mask.sum()),
            "positive_detected": bool(
                positive.any() and (predicted[positive] == bl.POSITIVE_CLASS_INDEX).all()
            ),
            "positive_probability": (
                [float(v) for v in probabilities[positive]] if positive.any() else []
            ),
            "positive_predicted_class": (
                [bl.CLASSES[int(v)] for v in predicted[positive]] if positive.any() else []
            ),
        }

    return EvaluationResult(
        scheme=scheme,
        model_name=model_name,
        evaluated_at_utc=datetime.now(tz=UTC),
        config_sha256=seal.config_sha256,
        n_test_windows=int(truth.size),
        n_test_groups=len(set(group_ids)),
        n_test_positives=int(binary.sum()),
        per_class=per_class,
        macro_f1=_interval(macro, groups, stat_macro),
        mass_movement_f1=_interval(mm_f1, groups, stat_mm(2)),
        mass_movement_precision=_interval(mm_precision, groups, stat_mm(0)),
        mass_movement_recall=_interval(mm_recall, groups, stat_mm(1)),
        roc_auc=_interval(roc_auc(binary, probabilities), groups, stat_auc),
        confusion=confusion_matrix(truth, predicted),
        confusion_by_region=by_region,
        reliability=reliability(probabilities, binary),
        forced_group_outcomes=outcomes,
        notes=list(notes),
    )


# --- paired comparison --------------------------------------------------------------------


class PairedComparison(BaseModel):
    """Deep minus baseline F1, with the interval that decides promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: str
    challenger: str
    incumbent: str
    challenger_f1: float
    incumbent_f1: float
    delta_f1: float
    delta_low: float
    delta_high: float
    n_resamples: int
    promoted: bool
    rule: str = (
        "The challenger becomes default only if the paired-bootstrap 95% lower bound on "
        "delta F1 exceeds 0. Fixed before either model was trained."
    )
    notes: list[str] = Field(default_factory=list)


def paired_bootstrap(
    *,
    scheme: str,
    challenger: str,
    incumbent: str,
    truth: np.ndarray,
    challenger_predicted: np.ndarray,
    incumbent_predicted: np.ndarray,
    groups: np.ndarray,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> PairedComparison:
    """Resample the same groups for both models, so their shared difficulty cancels."""
    truth = np.asarray(truth, dtype=int)
    a = np.asarray(challenger_predicted, dtype=int)
    b = np.asarray(incumbent_predicted, dtype=int)
    f1_a = _prf(truth, a, bl.POSITIVE_CLASS_INDEX)[2]
    f1_b = _prf(truth, b, bl.POSITIVE_CLASS_INDEX)[2]

    def delta(idx: np.ndarray) -> float:
        return (
            _prf(truth[idx], a[idx], bl.POSITIVE_CLASS_INDEX)[2]
            - _prf(truth[idx], b[idx], bl.POSITIVE_CLASS_INDEX)[2]
        )

    low, high = _bootstrap(groups, delta, iterations=iterations, seed=seed)
    return PairedComparison(
        scheme=scheme,
        challenger=challenger,
        incumbent=incumbent,
        challenger_f1=f1_a,
        incumbent_f1=f1_b,
        delta_f1=f1_a - f1_b,
        delta_low=low,
        delta_high=high,
        n_resamples=iterations,
        promoted=bool(np.isfinite(low) and low > 0.0),
    )
