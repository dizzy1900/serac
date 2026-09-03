"""The feature vector cannot encode geometry, epoch or identity, and honours `valid`."""

from __future__ import annotations

import numpy as np
import pytest

from serac.models.discriminator.features import (
    FEATURE_NAMES,
    FORBIDDEN_FEATURE_TOKENS,
    N_FEATURES,
    audit_feature_names,
    compute_features,
    feature_matrix,
)
from serac.models.discriminator.windows import COMPONENTS, MAX_STATIONS_PER_EVENT, N_SAMPLES


def _window(seed: int, n_stations: int = 5) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    waveform = rng.standard_normal((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES)) * 1e-6
    valid = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
    valid[:n_stations] = True
    return waveform.astype(np.float32), valid


def test_no_forbidden_feature_tokens() -> None:
    """The mechanical guard the brief asks for. An empty list is the only passing result."""
    assert audit_feature_names() == [], (
        "a feature name mentions geometry, epoch or identity; with ~320 positives those are "
        "close to a primary key and the model would memorise events instead of physics"
    )
    assert FORBIDDEN_FEATURE_TOKENS  # the guard must not be silently emptied


def test_feature_names_are_unique_and_fixed_length() -> None:
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES) == N_FEATURES


def test_features_are_deterministic() -> None:
    waveform, valid = _window(7)
    first = compute_features(waveform, valid)
    second = compute_features(waveform.copy(), valid.copy())
    assert first == second


def test_features_are_finite_and_complete() -> None:
    waveform, valid = _window(3)
    values = compute_features(waveform, valid)
    assert list(values) == list(FEATURE_NAMES)
    assert all(np.isfinite(v) for v in values.values())


def test_padded_slots_are_not_read() -> None:
    """Rewriting an invalid slot must not move a single feature."""
    waveform, valid = _window(11, n_stations=4)
    before = compute_features(waveform, valid)
    polluted = waveform.copy()
    polluted[valid.shape[0] - 1] = 1e9  # a slot whose `valid` row is all False
    after = compute_features(polluted, valid)
    assert before == after


def test_an_empty_window_yields_zeros_not_nans() -> None:
    waveform = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32)
    valid = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
    values = compute_features(waveform, valid)
    assert all(v == 0.0 for v in values.values())


def test_long_period_energy_raises_the_lp_sp_ratio() -> None:
    """The signature feature must move in the physically correct direction."""
    times = np.arange(N_SAMPLES) / 20.0
    long_period = np.sin(2 * np.pi * 0.02 * times)
    short_period = np.sin(2 * np.pi * 3.0 * times)
    valid = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
    valid[:3] = True

    def build(signal: np.ndarray) -> np.ndarray:
        waveform = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32)
        waveform[:3] = signal.astype(np.float32)
        return waveform

    lp = compute_features(build(long_period), valid)["vert_log_lp_sp_ratio_med"]
    sp = compute_features(build(short_period), valid)["vert_log_lp_sp_ratio_med"]
    assert lp > sp


def test_feature_matrix_shape() -> None:
    waveforms = np.stack([_window(i)[0] for i in range(3)])
    valids = np.stack([_window(i)[1] for i in range(3)])
    assert feature_matrix(waveforms, valids).shape == (3, N_FEATURES)


@pytest.mark.parametrize("token", FORBIDDEN_FEATURE_TOKENS)
def test_each_forbidden_token_is_actually_absent(token: str) -> None:
    assert not [n for n in FEATURE_NAMES if token in n.lower()]
