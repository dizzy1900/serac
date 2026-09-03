"""LightGBM three-class baseline: mass_movement / tectonic / noise.

This is the bar the deep model has to clear. It is deliberately small and boring — gradient
boosted trees on 79 hand-designed features — because a baseline whose behaviour can be read
off its feature importances is worth more here than an unexplainable model that scores a
little higher on twenty test positives.

**Calibration is fitted on validation only.** A classifier's raw margin is not a probability,
and `DetectionCandidate` refuses to carry a `probability` without a `probability_calibration`
for exactly that reason. A Platt (sigmoid) calibrator fitted on the training set would be
fitted on scores the trees have already memorised and would report near-certainty on
everything; fitted on validation it is fitted on scores from data the trees have not seen.
Test is never touched by the calibrator.

**The artifact records what the model was trained on.** `train_event_groups_sha256` is the
sha256 of the sorted, newline-joined training group ids. `validation/discriminator.py`
recomputes it from the split and compares, so the committed `model.txt` can *prove* which
groups it saw. A model claiming Chamoli was held out, whose hash says otherwise, fails the
gate — the claim is checkable rather than a comment.

`model.txt`, `calibrator.json` and `artifact.json` are committed so CI and the streaming
detector run offline with no training step.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator.features import FEATURE_NAMES, FEATURES_VERSION, N_FEATURES

if TYPE_CHECKING:  # pragma: no cover
    import lightgbm as lgb

BASELINE_VERSION = "0.1.0"
BASELINE_NAME = "lgbm-3class"

CLASSES: Final[tuple[str, ...]] = ("mass_movement", "tectonic", "noise")
POSITIVE_CLASS_INDEX: Final = 0

ARTIFACT_DIR: Final = Path("baselines/discriminator")
MODEL_FILE: Final = "model.txt"
CALIBRATOR_FILE: Final = "calibrator.json"
ARTIFACT_FILE: Final = "artifact.json"

# Fixed before the first test evaluation and sealed by `reports/m1/seal.json`. Deliberately
# small: 1900-odd training windows will not support a deep forest without memorising groups.
LGBM_PARAMS: Final[dict[str, Any]] = {
    "objective": "multiclass",
    "num_class": len(CLASSES),
    "learning_rate": 0.05,
    "num_leaves": 15,
    "max_depth": 5,
    "min_data_in_leaf": 30,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "seed": 20260903,
    "deterministic": True,
    "force_row_wise": True,
}
NUM_BOOST_ROUND: Final = 600
EARLY_STOPPING_ROUNDS: Final = 60


class BaselineError(SeracError):
    """The baseline could not be trained or loaded."""


class SigmoidCalibrator(BaseModel):
    """Platt scaling of the mass-movement margin, fitted on validation only.

    Stored as two numbers so the committed `calibrator.json` is auditable by eye: a reviewer
    can see the whole calibration, which is not true of an isotonic step function.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = "sigmoid"
    slope: float
    intercept: float
    fitted_on: str = Field(description="Which split the calibrator saw. Must be `val`.")
    n_fitted: int = Field(ge=0)

    def probability(self, raw: np.ndarray) -> np.ndarray:
        """Calibrated P(mass movement) from the raw one-vs-rest score."""
        return 1.0 / (
            1.0 + np.exp(-(self.slope * np.asarray(raw, dtype=np.float64) + self.intercept))
        )


class BaselineArtifact(BaseModel):
    """Everything needed to reproduce, verify and run the committed model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_version: str = BASELINE_VERSION
    name: str = BASELINE_NAME
    trained_at_utc: AwareDatetime
    features_version: str = FEATURES_VERSION
    feature_names: list[str]
    classes: list[str] = Field(default_factory=lambda: list(CLASSES))
    split_scheme: str
    params: dict[str, Any]
    best_iteration: int = Field(ge=0)
    n_train_windows: int = Field(ge=0)
    n_val_windows: int = Field(ge=0)
    n_train_groups: int = Field(ge=0)
    train_event_groups_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the sorted, newline-joined training group ids. Recomputed by "
        "the gate so the shipped model proves what it was trained on.",
    )
    train_event_groups: list[str] = Field(
        description="The training groups themselves, so the hash can be checked by hand."
    )
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibrator: SigmoidCalibrator
    class_weights: dict[str, float]
    notes: list[str] = Field(default_factory=list)


def group_hash(groups: list[str]) -> str:
    """sha256 of the sorted, newline-joined, de-duplicated group ids."""
    payload = "\n".join(sorted(set(groups))) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def balanced_class_weights(labels: np.ndarray) -> dict[int, float]:
    """`class_weight="balanced"`: n / (k * count_c), so the rare class is not ignored.

    Mass movements are a sixth of the windows by construction (1:5 negatives). Without this
    the trees can reach 83% accuracy by never predicting the class the component exists for.
    """
    counts = np.bincount(labels.astype(int), minlength=len(CLASSES)).astype(np.float64)
    total = float(labels.size)
    return {
        index: (total / (len(CLASSES) * count) if count > 0 else 0.0)
        for index, count in enumerate(counts)
    }


def _fit_sigmoid(
    scores: np.ndarray, targets: np.ndarray, iterations: int = 200
) -> tuple[float, float]:
    """Platt scaling by Newton-free gradient descent on the log loss.

    Written out rather than delegated to sklearn's `CalibratedClassifierCV` because that class
    refits the estimator internally, which would make "fitted on validation only" false.
    """
    x = np.asarray(scores, dtype=np.float64)
    y = np.asarray(targets, dtype=np.float64)
    # Platt's prior correction: pull the targets off 0/1 so a separable fold cannot drive the
    # slope to infinity and report probability 1.000 on everything.
    n_pos, n_neg = float(y.sum()), float((1 - y).sum())
    hi = (n_pos + 1.0) / (n_pos + 2.0) if n_pos > 0 else 0.5
    lo = 1.0 / (n_neg + 2.0) if n_neg > 0 else 0.5
    t = np.where(y > 0.5, hi, lo)
    slope, intercept, rate = 1.0, 0.0, 0.5
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-(slope * x + intercept)))
        residual = p - t
        grad_slope = float((residual * x).mean())
        grad_intercept = float(residual.mean())
        slope -= rate * grad_slope
        intercept -= rate * grad_intercept
    return slope, intercept


def _margin(raw: np.ndarray) -> np.ndarray:
    """One-vs-rest margin for the mass-movement class from LightGBM's raw scores."""
    raw = np.asarray(raw, dtype=np.float64)
    positive = raw[:, POSITIVE_CLASS_INDEX]
    others = np.delete(raw, POSITIVE_CLASS_INDEX, axis=1)
    # log-sum-exp of the rival classes: the margin is how much the positive class beats the
    # best explanation among the others, not merely its own score.
    rival = np.log(np.exp(others - others.max(axis=1, keepdims=True)).sum(axis=1)) + others.max(
        axis=1
    )
    return np.asarray(positive - rival, dtype=np.float64)


def train(
    features: np.ndarray,
    labels: np.ndarray,
    splits: np.ndarray,
    groups: list[str],
    *,
    split_scheme: str,
    artifact_dir: Path,
) -> BaselineArtifact:
    """Fit on train, early-stop and calibrate on val, touch test never.

    `splits` carries `train`/`val`/`test` per window. This function raises if it is handed a
    test row at all, because the safest way to guarantee the test set stayed unseen is for the
    training code to be structurally unable to see it.
    """
    from datetime import UTC, datetime

    import lightgbm as lgb

    if features.shape[1] != N_FEATURES:
        raise BaselineError(f"expected {N_FEATURES} features, got {features.shape[1]}")

    is_train = splits == "train"
    is_val = splits == "val"
    if not is_train.any() or not is_val.any():
        raise BaselineError(
            f"scheme {split_scheme}: train={int(is_train.sum())} val={int(is_val.sum())}; "
            "both folds must be non-empty"
        )

    weights = balanced_class_weights(labels[is_train])
    train_weight = np.array([weights[int(label)] for label in labels[is_train]])
    val_weight = np.array([weights[int(label)] for label in labels[is_val]])

    train_set = lgb.Dataset(
        features[is_train],
        label=labels[is_train],
        weight=train_weight,
        feature_name=list(FEATURE_NAMES),
        free_raw_data=False,
    )
    val_set = lgb.Dataset(
        features[is_val],
        label=labels[is_val],
        weight=val_weight,
        feature_name=list(FEATURE_NAMES),
        reference=train_set,
        free_raw_data=False,
    )
    booster = lgb.train(
        LGBM_PARAMS,
        train_set,
        num_boost_round=NUM_BOOST_ROUND,
        valid_sets=[val_set],
        valid_names=["val"],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / MODEL_FILE
    booster.save_model(str(model_path), num_iteration=booster.best_iteration)

    raw_val = booster.predict(
        features[is_val], num_iteration=booster.best_iteration, raw_score=True
    )
    slope, intercept = _fit_sigmoid(
        _margin(np.asarray(raw_val)), (labels[is_val] == POSITIVE_CLASS_INDEX).astype(float)
    )
    calibrator = SigmoidCalibrator(
        slope=slope, intercept=intercept, fitted_on="val", n_fitted=int(is_val.sum())
    )
    (artifact_dir / CALIBRATOR_FILE).write_text(
        calibrator.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    train_groups = sorted({g for g, keep in zip(groups, is_train, strict=True) if keep})
    artifact = BaselineArtifact(
        trained_at_utc=datetime.now(tz=UTC),
        feature_names=list(FEATURE_NAMES),
        split_scheme=split_scheme,
        params=dict(LGBM_PARAMS),
        best_iteration=int(booster.best_iteration or 0),
        n_train_windows=int(is_train.sum()),
        n_val_windows=int(is_val.sum()),
        n_train_groups=len(train_groups),
        train_event_groups_sha256=group_hash(train_groups),
        train_event_groups=train_groups,
        model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        calibrator=calibrator,
        class_weights={CLASSES[k]: v for k, v in weights.items()},
        notes=[
            "calibrator fitted on the validation fold only; the test fold was never scored "
            "during training, early stopping or calibration",
            "class weights are `balanced`: n / (k * count_c) on the training fold",
        ],
    )
    (artifact_dir / ARTIFACT_FILE).write_text(
        artifact.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return artifact


class LoadedBaseline:
    """A committed model plus its calibrator, ready to score. No training dependency."""

    def __init__(self, booster: lgb.Booster, artifact: BaselineArtifact) -> None:
        self.booster = booster
        self.artifact = artifact

    @property
    def calibrator(self) -> SigmoidCalibrator:
        return self.artifact.calibrator

    def class_probabilities(self, features: np.ndarray) -> np.ndarray:
        """(n, 3) softmax probabilities in `CLASSES` order."""
        return np.asarray(self.booster.predict(features), dtype=np.float64).reshape(
            -1, len(CLASSES)
        )

    def calibrated_probability(self, features: np.ndarray) -> np.ndarray:
        """(n,) calibrated P(mass movement).

        The only number that may reach a `DetectionCandidate.probability`.
        """
        raw = np.asarray(self.booster.predict(features, raw_score=True), dtype=np.float64)
        return self.calibrator.probability(_margin(raw.reshape(-1, len(CLASSES))))


def load(artifact_dir: Path) -> LoadedBaseline:
    """Load the committed artifact. Raises rather than training a fallback."""
    import lightgbm as lgb

    model_path = artifact_dir / MODEL_FILE
    artifact_path = artifact_dir / ARTIFACT_FILE
    if not model_path.exists() or not artifact_path.exists():
        raise BaselineError(
            f"no committed baseline at {artifact_dir}; expected {MODEL_FILE} and {ARTIFACT_FILE}"
        )
    artifact = BaselineArtifact.model_validate(json.loads(artifact_path.read_text("utf-8")))
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual != artifact.model_sha256:
        raise BaselineError(
            f"{MODEL_FILE} sha256 {actual} does not match the artifact's {artifact.model_sha256}; "
            "the model and its provenance record have diverged"
        )
    return LoadedBaseline(lgb.Booster(model_file=str(model_path)), artifact)
