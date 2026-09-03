"""`RunoutSurrogate.predict()` must produce a `CascadeForecast` the contract accepts.

Trained on a tiny in-process dataset: the point is the contract and the provenance of every
number, not the model's accuracy, which is measured in `reports/runout/surrogate_metrics.json`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import torch

from serac.domain.common import iter_ranges
from serac.domain.forecast import CascadeForecast, ConfidenceTier, ModelProvenance
from serac.models.runout.forecast import MODEL_SOURCE_REF, RunoutSurrogate
from serac.models.runout.surrogate import Dataset, corridor_features, parameter_vector
from serac.models.runout.training import train

CHAINAGE = np.arange(400) * 250.0 + 125.0
BED = 4000.0 * np.exp(-CHAINAGE / 40000.0) + 400.0
TRANSECTS = ["rasuwagadhi-gyirong", "syabrubesi", "betrawati", "galchhi"]


def _parameters(mu: float = 0.04, volume: float = 8.0e7) -> dict[str, object]:
    return {
        "release_volume_m3": volume,
        "ice_fraction": 0.5,
        "release_elevation_band_m": (3800.0, 4400.0),
        "entrainment_coefficient": 0.005,
        "mu": mu,
        "xi_m_s2": 1000.0,
    }


@pytest.fixture(scope="module")
def surrogate() -> RunoutSurrogate:
    rng = np.random.default_rng(0)
    static = corridor_features(BED, CHAINAGE)
    bins = [int(np.argmin(np.abs(CHAINAGE - c))) for c in (16875.0, 31121.0, 62371.0, 96975.0)]
    params, depths, arrivals, reached = [], [], [], []
    for _ in range(32):
        mu = float(np.exp(rng.uniform(np.log(0.02), np.log(0.30))))
        params.append(parameter_vector(_parameters(mu=mu), 30.0))
        wet = 40.0 * (0.03 / mu) > CHAINAGE / 1000.0
        depth = np.where(wet, 20.0 * np.exp(-CHAINAGE / 30000.0), 0.0).astype(np.float32)
        arrival = np.where(wet, CHAINAGE / 20.0, 0.0).astype(np.float32)
        depths.append(depth)
        arrivals.append(arrival)
        reached.append(wet.astype(np.float32))
    data = Dataset(
        run_ids=[f"m{i:04d}" for i in range(32)],
        parameters=np.stack(params),
        static=static,
        max_depth=np.stack(depths),
        arrival=np.stack(arrivals),
        reached=np.stack(reached),
        transect_arrival=np.stack(
            [np.where(r[bins] > 0, a[bins], np.nan) for a, r in zip(arrivals, reached, strict=True)]
        ).astype(np.float32),
        transect_peak_stage=np.stack([d[bins] for d in depths]).astype(np.float32),
        transect_ids=TRANSECTS,
    )
    trained = train(data, epochs=20, device="cpu")
    return RunoutSurrogate(
        trained.fno,
        trained.regressor,
        trained.standardiser,
        trained.static,
        depth_scale=trained.depth_scale,
        arrival_scale=trained.arrival_scale,
        transect_ids=trained.transect_ids,
        chainage_m=CHAINAGE,
        bed_min_m=BED,
        design_hash="testhash",
    )


def _forecast(surrogate: RunoutSurrogate, **kwargs: object) -> CascadeForecast:
    origin = datetime(2026, 8, 26, 2, 52, 10, tzinfo=UTC)
    return surrogate.predict(
        _parameters(),
        forecast_id="test-forecast-0001",
        origin_time_utc=origin,
        issued_utc=origin + timedelta(minutes=3),
        **kwargs,  # type: ignore[arg-type]
    )


def test_predict_returns_a_valid_cascade_forecast(surrogate: RunoutSurrogate) -> None:
    forecast = _forecast(surrogate)

    assert isinstance(forecast, CascadeForecast)
    assert forecast.model.provenance == ModelProvenance.surrogate
    assert forecast.confidence_tier == ConfidenceTier.low
    # a full round trip through the published contract
    CascadeForecast.model_validate_json(forecast.model_dump_json())


def test_every_range_comes_from_the_model_and_is_ordered(surrogate: RunoutSurrogate) -> None:
    forecast = _forecast(surrogate)

    ranges = list(iter_ranges(forecast))
    assert ranges, "a forecast with no Range is not a forecast"
    for path, value in ranges:
        assert value.low <= value.high, path
        assert value.best is not None and value.low <= value.best <= value.high, path
        assert value.source_refs == [MODEL_SOURCE_REF], (
            f"{path}: a model output's source is the model run, never a document"
        )


def test_assumptions_carry_the_disclaimer_and_the_named_limitations(
    surrogate: RunoutSurrogate,
) -> None:
    forecast = _forecast(surrogate)
    joined = "\n".join(forecast.assumptions)

    assert "NOT r.avaflow" in joined
    assert "cross-validation" in joined.lower()
    assert "phase separation" in joined
    assert "under 60 m wide" in joined
    assert "erodible_depth" in joined
    assert "biased late" in joined
    assert "cascade rules v0" in joined


def test_damming_probability_is_bounded_and_labelled_v0(surrogate: RunoutSurrogate) -> None:
    forecast = _forecast(surrogate)
    if forecast.damming is None:
        pytest.skip("no constriction found on the synthetic corridor")

    probability = forecast.damming.probability
    assert probability.unit == "probability"
    assert 0.0 <= probability.low <= probability.high <= 1.0
    assert probability.notes is not None
    assert "not a probability" in probability.notes
    assert "cascade rules v0" in probability.notes


def test_an_unreached_transect_is_omitted_rather_than_given_a_made_up_time(
    surrogate: RunoutSurrogate,
) -> None:
    """The model saying "the flow does not get here" must not become an arrival time."""
    forecast = _forecast(surrogate, reach_threshold=0.999999)

    assert forecast.transect_arrivals == []


def test_source_volume_is_declared_as_an_input_not_a_prediction(
    surrogate: RunoutSurrogate,
) -> None:
    forecast = _forecast(surrogate)
    notes = forecast.source_volume_m3.notes or ""
    assert "input to the surrogate, not a prediction" in notes
    assert forecast.source_volume_m3.best == pytest.approx(8.0e7)


def test_inference_is_far_inside_the_latency_gate(surrogate: RunoutSurrogate) -> None:
    surrogate.infer(_parameters())  # warm up
    prediction = surrogate.infer(_parameters())
    assert prediction.latency_s < 2.0


# -- regression on the quantile-head defect ----------------------------------------------------


def test_lowest_quantile_can_emit_exactly_zero() -> None:
    """The 5th-percentile head must be able to say "dry", or dry bins can never be covered.

    Regression on the defect that put depth interval coverage at 0.1208 against a 0.85-0.95
    target: `cumsum(softplus(raw))` makes every quantile strictly positive, and max depth is zero
    over most of the corridor, so no amount of training could have covered a dry bin. The lowest
    quantile is a ReLU precisely so that it can reach zero.
    """
    from serac.models.runout.surrogate import QUANTILES, _monotone_quantiles

    raw = torch.tensor([[[-5.0, -5.0], [0.0, 3.0], [1.0, -2.0]]])  # (batch, quantile, bin)
    assert raw.shape[1] == len(QUANTILES)

    out = _monotone_quantiles(raw, dim=1)

    assert float(out[0, 0, 0]) == 0.0, "a strongly negative logit must give exactly zero"
    assert (out >= 0.0).all(), "quantiles are depths and cannot be negative"
    assert (out[:, 1:] >= out[:, :-1]).all(), "quantiles must not cross"


def test_trained_heads_actually_emit_zero_somewhere(surrogate: RunoutSurrogate) -> None:
    """Not just representable in principle: the trained model must use it."""
    prediction = surrogate.infer(_parameters(mu=0.29))
    assert float(prediction.max_depth_q[0].min()) == 0.0
