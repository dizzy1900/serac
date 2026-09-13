"""The v1 autoencoder is an interface. It does not invent a reconstruction error.

All series here are constructed in the test. Nothing is read from the network or from `data/`.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from serac.domain.watch import WatchTier
from serac.domain.watch_model import (
    DEFAULT_AUTOENCODER_LAYERS,
    SELF_SUPERVISED_CUBE_RECONSTRUCTION,
    AutoencoderSpec,
    UnitWatchScore,
    WatchScoreInsufficientReason,
)
from serac.models.watch.aggregate import days_since_epoch, epoch_plus
from serac.models.watch.anomaly import UnitSeries, walk_forward
from serac.models.watch.autoencoder import (
    CUBE_AUTOENCODER_NOTES,
    CubeAutoencoderWatch,
    NotFittedError,
)
from serac.models.watch.robust_z import ROBUST_Z_NAME, RobustZWatch
from serac.ports.watch import (
    FeatureCubeView,
    InMemoryCubeView,
    LayerSeries,
    WatchAnomalyModel,
    causal_max_input_time,
)

AS_OF = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2021, 1, 1, tzinfo=UTC)
PAST = datetime(2019, 6, 1, tzinfo=UTC)
EARLIER = datetime(2018, 1, 1, tzinfo=UTC)


def _layer(times: tuple[datetime, ...], values: tuple[float, ...]) -> LayerSeries:
    return LayerSeries(times=times, values=values)


def _feature_view(
    unit_id: str = "su-00",
    *,
    times: tuple[datetime, ...] = (EARLIER, PAST, FUTURE),
    values: tuple[float, ...] = (0.0, 0.0, 99.0),
) -> InMemoryCubeView:
    layers = {name: _layer(times, values) for name in DEFAULT_AUTOENCODER_LAYERS}
    return InMemoryCubeView(layers={unit_id: layers})


def _series(
    unit_id: str,
    *,
    n: int = 160,
    step_days: float = 12.0,
    rate_mm_yr: float = 0.0,
    coherence: float = 0.7,
    sensitivity: float = 0.8,
) -> UnitSeries:
    t = np.arange(n, dtype=np.float64) * step_days
    signal = rate_mm_yr * t / 365.25
    return UnitSeries(
        unit_id=unit_id,
        t_days=t,
        los_mm=signal * sensitivity,
        coherence=np.full(n, coherence),
        los_sensitivity_signed=sensitivity,
    )


def _view_from_series(series_by_unit: dict[str, UnitSeries]) -> InMemoryCubeView:
    layers: dict[str, dict[str, LayerSeries]] = {}
    static: dict[str, dict[str, float]] = {}
    flags: dict[str, dict[str, bool]] = {}
    for unit_id, series in series_by_unit.items():
        times = tuple(epoch_plus(float(t)) for t in series.t_days)
        layers[unit_id] = {
            "los_displacement": _layer(times, tuple(float(v) for v in series.los_mm)),
            "temporal_coherence": _layer(times, tuple(float(v) for v in series.coherence)),
        }
        static[unit_id] = {"los_sensitivity_signed": series.los_sensitivity_signed}
        flags[unit_id] = {"inside_footprint": series.inside_footprint}
    return InMemoryCubeView(layers=layers, static=static, flags=flags)


def test_both_v0_and_v1_implement_the_port() -> None:
    v0: WatchAnomalyModel = RobustZWatch()
    v1: WatchAnomalyModel = CubeAutoencoderWatch()
    assert isinstance(v0, WatchAnomalyModel)
    assert isinstance(v1, WatchAnomalyModel)
    assert v0.required_layers == ("los_displacement", "temporal_coherence")
    assert v1.required_layers == DEFAULT_AUTOENCODER_LAYERS
    assert v0.info().is_fitted is True
    assert v1.info().is_fitted is False
    assert CUBE_AUTOENCODER_NOTES in (v1.info().notes or "")


def test_unfitted_autoencoder_never_returns_quiet() -> None:
    """A cube of zeros is not stability. No weights means not assessed."""
    model = CubeAutoencoderWatch()
    view = _feature_view(values=(0.0, 0.0, 0.0))
    score = model.score_unit("su-00", AS_OF, view)
    assert score.tier is WatchTier.insufficient_data
    assert score.tier is not WatchTier.quiet
    assert score.score is None
    assert score.insufficient_reason is WatchScoreInsufficientReason.not_fitted
    assert score.model_name == "cube-autoencoder-v1"


def test_a_weights_path_does_not_fake_a_fitted_model(tmp_path: Path) -> None:
    path = tmp_path / "weights.npy"
    path.write_bytes(b"not a trained model")
    model = CubeAutoencoderWatch(AutoencoderSpec.v1_defaults(), weights_path=path)
    score = model.score_unit("su-00", AS_OF, _feature_view())
    assert model.info().is_fitted is False
    assert model.info().weights_path == str(path)
    assert score.tier is WatchTier.insufficient_data
    assert score.score is None
    assert score.insufficient_reason is WatchScoreInsufficientReason.not_fitted


def test_encode_and_decode_raise_when_not_fitted() -> None:
    model = CubeAutoencoderWatch()
    window = np.zeros((4, len(DEFAULT_AUTOENCODER_LAYERS)), dtype=np.float64)
    with pytest.raises(NotFittedError, match="no model has been trained"):
        model.encode(window)
    with pytest.raises(NotFittedError, match="no model has been trained"):
        model.decode(np.zeros(16, dtype=np.float64))


def test_causality_max_input_time_is_at_or_before_as_of() -> None:
    model = CubeAutoencoderWatch()
    view = _feature_view()
    score = model.score_unit("su-00", AS_OF, view)
    assert score.max_input_time is not None
    assert score.max_input_time <= score.as_of
    assert score.max_input_time == PAST
    assert causal_max_input_time(view, "su-00", model.required_layers, AS_OF) == PAST


def test_a_sample_exactly_at_as_of_is_legal() -> None:
    """Inclusive trailing window: t <= as_of, matching anomaly.py."""
    view = _feature_view(times=(EARLIER, AS_OF, FUTURE), values=(1.0, 2.0, 99.0))
    score = CubeAutoencoderWatch().score_unit("su-00", AS_OF, view)
    assert score.max_input_time == AS_OF


def test_future_only_samples_leave_max_input_time_null_and_still_not_quiet() -> None:
    view = _feature_view(times=(FUTURE,), values=(0.0,))
    score = CubeAutoencoderWatch().score_unit("su-00", AS_OF, view)
    assert score.max_input_time is None
    assert score.tier is WatchTier.insufficient_data
    assert score.score is None


def test_v0_wrapper_is_causal_and_matches_walk_forward() -> None:
    series = {f"su-{i:02d}": _series(f"su-{i:02d}", rate_mm_yr=float(i)) for i in range(12)}
    as_of = epoch_plus(float(next(iter(series.values())).t_days[-1]))
    expected = walk_forward(series, [float(t) for t in next(iter(series.values())).t_days])
    view = _view_from_series(series)
    got = RobustZWatch().score_unit("su-00", as_of, view)
    raw = expected[-1]["su-00"]
    assert got.model_name == ROBUST_Z_NAME
    assert got.tier is WatchTier(raw.tier.value)
    assert got.score == pytest.approx(raw.score)
    assert got.max_input_time is not None
    assert got.max_input_time <= as_of
    assert days_since_epoch(got.max_input_time) == pytest.approx(
        float(next(iter(series.values())).t_days[-1])
    )


def test_v0_wrapper_ignores_future_samples() -> None:
    series = {f"su-{i:02d}": _series(f"su-{i:02d}", rate_mm_yr=float(i)) for i in range(12)}
    as_of = epoch_plus(float(next(iter(series.values())).t_days[-1]))
    honest = RobustZWatch().score_unit("su-00", as_of, _view_from_series(series))

    leaked: dict[str, UnitSeries] = {}
    for unit_id, unit in series.items():
        future_t = unit.t_days[-1] + np.arange(1, 20, dtype=np.float64) * 12.0
        leaked[unit_id] = UnitSeries(
            unit_id=unit_id,
            t_days=np.concatenate([unit.t_days, future_t]),
            los_mm=np.concatenate([unit.los_mm, np.full(future_t.size, 5000.0)]),
            coherence=np.concatenate([unit.coherence, np.full(future_t.size, 0.95)]),
            los_sensitivity_signed=unit.los_sensitivity_signed,
        )
    peeked = RobustZWatch().score_unit("su-00", as_of, _view_from_series(leaked))
    assert peeked.score == honest.score
    assert peeked.tier is honest.tier
    assert peeked.max_input_time == honest.max_input_time
    assert peeked.max_input_time is not None
    assert peeked.max_input_time <= as_of


def test_v0_too_few_samples_is_insufficient_not_quiet() -> None:
    series = {"su-00": _series("su-00", n=3)}
    as_of = epoch_plus(float(series["su-00"].t_days[-1]))
    score = RobustZWatch().score_unit("su-00", as_of, _view_from_series(series))
    assert score.tier is WatchTier.insufficient_data
    assert score.tier is not WatchTier.quiet
    assert score.score is None
    assert score.insufficient_reason is WatchScoreInsufficientReason.too_few_samples


def test_v0_missing_unit_is_insufficient() -> None:
    series = {"su-00": _series("su-00")}
    as_of = epoch_plus(float(series["su-00"].t_days[-1]))
    score = RobustZWatch().score_unit("su-99", as_of, _view_from_series(series))
    assert score.insufficient_reason is WatchScoreInsufficientReason.unit_not_in_view
    assert score.tier is WatchTier.insufficient_data


def test_spec_declares_self_supervised_protocol_and_unused_labels() -> None:
    spec = AutoencoderSpec.v1_defaults()
    assert spec.training_protocol == SELF_SUPERVISED_CUBE_RECONSTRUCTION
    assert spec.reconstruction_metric == "masked_mse"
    assert spec.layers == DEFAULT_AUTOENCODER_LAYERS
    assert spec.latent_dim == 16


def test_unit_watch_score_rejects_a_peek_past_as_of() -> None:
    with pytest.raises(ValidationError, match="max_input_time"):
        UnitWatchScore(
            unit_id="su-00",
            as_of=AS_OF,
            score=None,
            tier=WatchTier.insufficient_data,
            insufficient_reason=WatchScoreInsufficientReason.not_fitted,
            model_name="cube-autoencoder-v1",
            model_version="0.1.0",
            max_input_time=AS_OF + timedelta(days=1),
        )


def test_unit_watch_score_cannot_express_a_predicted_date() -> None:
    names = set(UnitWatchScore.model_fields)
    assert not [n for n in names if "date" in n or "probability" in n or "forecast" in n]
    with pytest.raises(ValidationError):
        UnitWatchScore(
            unit_id="su-00",
            as_of=AS_OF,
            score=None,
            tier=WatchTier.insufficient_data,
            insufficient_reason=WatchScoreInsufficientReason.not_fitted,
            model_name="x",
            model_version="0.1.0",
            failure_date=AS_OF,  # type: ignore[call-arg]
        )


def test_unassessed_score_cannot_be_zero() -> None:
    with pytest.raises(ValidationError, match="has no score"):
        UnitWatchScore(
            unit_id="su-00",
            as_of=AS_OF,
            score=0.0,
            tier=WatchTier.insufficient_data,
            insufficient_reason=WatchScoreInsufficientReason.not_fitted,
            model_name="cube-autoencoder-v1",
            model_version="0.1.0",
        )


def test_autoencoder_module_does_not_import_torch() -> None:
    import serac.models.watch.autoencoder as module

    source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "torch" not in imported
    assert "interface only; no model has been trained in this repository" in (
        (module.__doc__ or "").lower()
    )


def test_cube_view_is_the_port_input_type() -> None:
    view = _feature_view()
    assert isinstance(view, FeatureCubeView)
    assert "su-00" in view.units()


def test_v0_early_as_of_is_insufficient_not_quiet() -> None:
    """The wrapper uses walk_forward, so the pre-registered floors still apply."""
    series = {f"su-{i}": _series(f"su-{i}", n=40, rate_mm_yr=float(i)) for i in range(8)}
    # 24th sample: unit_state becomes measurable, but history of measurable states is empty.
    as_of = epoch_plus(float(next(iter(series.values())).t_days[23]))
    score = RobustZWatch().score_unit("su-0", as_of, _view_from_series(series))
    assert score.tier is WatchTier.insufficient_data
    assert score.tier is not WatchTier.quiet
    assert score.score is None
    assert score.insufficient_reason in {
        WatchScoreInsufficientReason.too_little_history,
        WatchScoreInsufficientReason.too_few_samples,
    }
