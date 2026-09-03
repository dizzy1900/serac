"""The detector refuses counts and never locates; the seal refuses a changed configuration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from serac.domain.seismic import SeismicTrace, Sncl, TraceEncoding, TraceProvenance
from serac.models.discriminator import evaluate as ev
from serac.models.discriminator import streaming as st
from serac.models.discriminator.baseline import (
    BaselineArtifact,
    LoadedBaseline,
    SigmoidCalibrator,
)
from serac.models.discriminator.features import FEATURE_NAMES

SOURCE = Path("src/serac/models/discriminator")


def _chunk(channel: str = "BHZ") -> SeismicTrace:
    samples = (np.random.default_rng(0).standard_normal(1000) * 100).astype("<f4")
    start = datetime(2026, 8, 26, 2, 51, tzinfo=UTC)
    return SeismicTrace(
        trace_id=f"t-{channel}",
        sncl=Sncl(network="NK", station="KKN", location="", channel=channel),
        start_time_utc=start,
        end_time_utc=start + timedelta(seconds=50),
        sampling_rate_hz=20.0,
        npts=1000,
        encoding=TraceEncoding.float32le,
        data=samples.tobytes(),
        data_sha256=hashlib.sha256(samples.tobytes()).hexdigest(),
        sequence=1,
        provenance=TraceProvenance(
            source="fixture", retrieved_at=start, fixture_path="tests/unit/discriminator"
        ),
    )


class _FakeBooster:
    def predict(self, features, **kwargs):
        rows = np.asarray(features).shape[0]
        return np.tile(np.array([2.0, 0.0, 0.0]), (rows, 1))


def _fake_model() -> LoadedBaseline:
    calibrator = SigmoidCalibrator(slope=1.0, intercept=0.0, fitted_on="val", n_fitted=100)
    artifact = BaselineArtifact(
        trained_at_utc=datetime.now(tz=UTC),
        feature_names=list(FEATURE_NAMES),
        split_scheme="loro_hma",
        params={},
        best_iteration=1,
        n_train_windows=10,
        n_val_windows=5,
        n_train_groups=3,
        train_event_groups_sha256="a" * 64,
        train_event_groups=["g1", "g2", "g3"],
        model_sha256="b" * 64,
        calibrator=calibrator,
        class_weights={"mass_movement": 1.0},
    )
    return LoadedBaseline(_FakeBooster(), artifact)  # type: ignore[arg-type]


def test_ingesting_without_a_response_raises_before_any_sample_lands() -> None:
    """Scoring counts would produce a confident, meaningless probability. Refuse loudly."""
    detector = st.DiscriminatorDetector(model=_fake_model(), require_response=True)
    with pytest.raises(st.ResponseRequiredError, match="require_response=True"):
        detector.ingest(_chunk())
    assert detector.chunks_seen == 0


def test_require_response_false_is_available_but_must_be_asked_for() -> None:
    detector = st.DiscriminatorDetector(model=_fake_model(), require_response=False)
    detector.ingest(_chunk())
    assert detector.chunks_seen == 1


def test_the_detector_reports_itself_as_not_a_stub_and_carries_its_model_hash() -> None:
    info = st.DiscriminatorDetector(model=_fake_model(), require_response=False).info()
    assert info.is_stub is False
    assert info.model_sha256 == "b" * 64
    assert info.calibration == "sigmoid"
    assert info.params["require_response"] is False


def test_poll_returns_nothing_before_enough_receivers_have_arrived() -> None:
    detector = st.DiscriminatorDetector(model=_fake_model(), require_response=False)
    detector.ingest(_chunk())
    assert detector.poll(datetime(2026, 8, 26, 3, 5, tzinfo=UTC)) == []


def test_the_detector_never_emits_a_location() -> None:
    """M1 says what kind of source, not where. Locating is M2's job."""
    source = (SOURCE / "streaming.py").read_text(encoding="utf-8")
    assert "source_location=None" in source
    assert "DetectionLocation(" not in source


def test_seisbench_models_is_never_imported() -> None:
    """SeisBench's models are phase pickers, not classifiers; loading one would be a category
    error dressed up as transfer learning."""
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "seisbench.models" in stripped:
                pytest.fail(f"{path}: {stripped}")
            if stripped.startswith(("import ", "from ")) and "seisbench" in stripped:
                assert "generate" in stripped or "data" in stripped, (
                    f"{path}: only seisbench's data side may be imported: {stripped}"
                )


def test_detector_stub_is_untouched() -> None:
    """`validate-stream` asserts on the stub's own source text; it stays byte-identical."""
    stub = Path("src/serac/streaming/detector_stub.py").read_text(encoding="utf-8")
    assert stub.startswith('"""STUB — replaced in Prompt 2.')
    assert "threshold_is_placeholder" in stub


def test_the_seal_is_written_once_and_refuses_a_changed_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ev.check_seal(tmp_path, "loro_hma")
    assert (tmp_path / ev.SEAL_PATH).exists()
    assert first.config_sha256 == ev.config_hash()

    # Same config, second scheme: allowed, and recorded.
    second = ev.check_seal(tmp_path, "time_forward")
    assert second.schemes_evaluated == ["loro_hma", "time_forward"]

    # A tuned constant: refused.
    monkeypatch.setattr(ev.bl, "LGBM_PARAMS", dict(ev.bl.LGBM_PARAMS) | {"num_leaves": 63})
    with pytest.raises(ev.SealBrokenError, match="configuration changed"):
        ev.check_seal(tmp_path, "loro_hma")


def test_the_seal_records_every_tunable_constant() -> None:
    fingerprint = ev.config_fingerprint()
    assert set(fingerprint) >= {
        "features",
        "windows",
        "catalog",
        "splits",
        "baseline",
        "bootstrap",
    }
    assert json.dumps(fingerprint, sort_keys=True)  # must be serialisable for hashing


def test_bootstrap_resamples_groups_not_windows() -> None:
    """Six windows of one event are one observation, not six."""
    truth = np.array([0] * 6 + [1] * 6)
    predicted = truth.copy()
    groups = np.array(["g1"] * 6 + ["g2"] * 6)
    low, high = ev._bootstrap(
        groups, lambda idx: ev._prf(truth[idx], predicted[idx], 0)[2], iterations=200
    )
    # With two groups and a perfect model, a resample can draw g2 twice and lose class 0
    # entirely, so the interval must reach 0. Window-level resampling could not do that.
    assert low == pytest.approx(0.0, abs=1e-9)
    assert high == pytest.approx(1.0, abs=1e-9)


def test_roc_auc_of_a_constant_score_is_a_half() -> None:
    assert ev.roc_auc(np.array([1, 0, 1, 0]), np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)


def test_paired_bootstrap_promotion_needs_a_lower_bound_above_zero() -> None:
    truth = np.array([0, 1, 2] * 8)
    same = truth.copy()
    comparison = ev.paired_bootstrap(
        scheme="loro_hma",
        challenger="deep",
        incumbent="baseline",
        truth=truth,
        challenger_predicted=same,
        incumbent_predicted=same,
        groups=np.array([f"g{i // 3}" for i in range(truth.size)]),
        iterations=200,
    )
    assert comparison.delta_f1 == 0.0
    assert comparison.promoted is False
