"""The committed artifacts load offline and prove what they were trained on."""

from __future__ import annotations

import json

import numpy as np
import pytest
from tests.conftest import REPO_ROOT

from serac.models.discriminator import baseline as bl
from serac.models.discriminator.catalog import FORCED_TEST_GROUPS
from serac.models.discriminator.features import FEATURE_NAMES, N_FEATURES

SCHEMES = ("loro_hma", "time_forward")


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_committed_baseline_loads_with_no_network_and_no_training(scheme: str) -> None:
    model = bl.load(REPO_ROOT / bl.ARTIFACT_DIR / scheme)
    assert model.artifact.split_scheme == scheme
    assert model.artifact.feature_names == list(FEATURE_NAMES)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_artifact_hash_matches_the_committed_model_file(scheme: str) -> None:
    """A model and its provenance record that disagree must not load at all."""
    model = bl.load(REPO_ROOT / bl.ARTIFACT_DIR / scheme)
    assert len(model.artifact.model_sha256) == 64


@pytest.mark.parametrize("scheme", SCHEMES)
def test_no_forced_test_group_is_in_the_committed_training_set(scheme: str) -> None:
    artifact = bl.load(REPO_ROOT / bl.ARTIFACT_DIR / scheme).artifact
    assert not set(artifact.train_event_groups) & FORCED_TEST_GROUPS
    assert artifact.train_event_groups_sha256 == bl.group_hash(artifact.train_event_groups)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_calibrator_was_fitted_on_validation_only(scheme: str) -> None:
    model = bl.load(REPO_ROOT / bl.ARTIFACT_DIR / scheme)
    assert model.calibrator.fitted_on == "val"
    assert model.calibrator.n_fitted > 0


def test_the_committed_model_scores_and_returns_a_probability_in_range() -> None:
    model = bl.load(REPO_ROOT / bl.ARTIFACT_DIR / "loro_hma")
    features = np.zeros((3, N_FEATURES), dtype=np.float64)
    probabilities = model.class_probabilities(features)
    assert probabilities.shape == (3, len(bl.CLASSES))
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    calibrated = model.calibrated_probability(features)
    assert calibrated.shape == (3,)
    assert np.all((calibrated >= 0) & (calibrated <= 1))


def test_a_tampered_model_file_is_refused(tmp_path) -> None:
    """The artifact records the model's sha256; a divergence must be fatal, not a warning."""
    source = REPO_ROOT / bl.ARTIFACT_DIR / "loro_hma"
    for name in (bl.MODEL_FILE, bl.CALIBRATOR_FILE, bl.ARTIFACT_FILE):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    (tmp_path / bl.MODEL_FILE).write_bytes(
        (source / bl.MODEL_FILE).read_bytes() + b"\n# tampered\n"
    )
    with pytest.raises(bl.BaselineError, match="diverged"):
        bl.load(tmp_path)


def test_the_langtang_case_study_is_labelled_as_a_case_study_not_a_metric() -> None:
    path = REPO_ROOT / "reports" / "m1" / "case_study_langtang-lhende-2026.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["below_the_datasets_quality_bar"] is True
    assert "not a test-set metric" in record["caveat"]
    assert record["model"]["event_group_in_training"] is False


def test_the_paired_comparison_records_the_promotion_rule_and_its_outcome() -> None:
    path = REPO_ROOT / "reports" / "m1" / "paired_loro_hma.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert "lower bound" in record["rule"]
    assert record["promoted"] is (record["delta_low"] > 0.0)
