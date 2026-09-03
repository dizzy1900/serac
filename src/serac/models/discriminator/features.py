"""Fixed-length feature vector for one window, computed only from waveform and valid.

`compute_features(waveform, valid)` takes the two Zarr arrays and nothing else. It never sees
an event id, a coordinate, a magnitude, a date or a station code, because it is not given
them: the signature is the enforcement. `FORBIDDEN_FEATURE_TOKENS` is the second layer, a test
over the emitted names, and the two together are why a feature cannot quietly encode identity.

**Why geometry is banned rather than merely discouraged.** With ~320 positives, epicentral
distance and azimuth are close to a primary key. A model given them can memorise "this pattern
of distances is Chamoli" and score perfectly on the test set while having learned nothing about
single-force sources. The same is true of the origin year (instrument generations changed) and
of station codes (a Nepali broadband appears almost exclusively under positives). Every one of
those is a shortcut that looks like skill.

**No geometry-derived feature is kept.** The brief allows one with an explicit audit; the
audit is that none was needed. Incidence angle and back-azimuth-corrected polarisation were
both considered and rejected: they are functions of source-receiver geometry, and a model that
had them could reconstruct the epicentre. `report_ablation` therefore has nothing to ablate,
and `evaluate.py` records that as the finding rather than inventing a feature to remove.

The physics being measured, in order:

* **long-period to short-period energy ratio.** The signature. A mass movement is a
  single-force source whose energy sits at 20-100 s; a double-couple earthquake of the same
  amplitude radiates far more above 1 Hz.
* **envelope duration and emergence.** Mass movements are long and emergent, tens to hundreds
  of seconds with no impulsive onset; earthquakes have a sharp P arrival.
* **centroid-frequency drift ("triangularity").** As a slide accelerates and then deposits,
  the spectral centroid drifts down and back; an earthquake's does not.
* **horizontal-to-vertical energy ratio and long-period rectilinearity.** A single force is
  more linearly polarised at long period than a double-couple source at the same distance.
* **cross-trace envelope coherence.** A real source produces envelopes with a common shape at
  every receiver; incoherent envelopes are noise.

**What was removed, and why.** An early version carried `valid_channel_fraction`, the share of
the 12x3 slots holding usable data. It looks like a harmless quality flag and it is not one:
how many receivers a window has tracks network density, which tracks region and epoch, so a
model could use it to infer where and when an event happened. It was dropped before the test
set was scored.

**Removing it does not fully close the channel, and this docstring will not pretend it does.**
The cross-trace aggregates below (`*_mad`, `*_p90`) and `lp_envelope_coherence` are computed
over however many traces contributed, so their sampling behaviour still carries a trace of the
count. Measured on the built store: corr(`n_stations`, positive) = +0.110 over all windows,
and `n_stations` alone separates the classes at ROC-AUC 0.587 -- better than chance. What was
removed was the direct, explicit feature; the residual is measured, reported by
`validate-discriminator` as `receiver_count_symmetry_between_classes`, and written up in the
model card's failure modes.
"""

from __future__ import annotations

from typing import Final

import numpy as np

from serac.models.discriminator.windows import N_SAMPLES, TARGET_SAMPLING_RATE_HZ

FEATURES_VERSION = "0.1.0"

# Any of these appearing in a feature name fails `test_no_forbidden_feature_tokens`.
FORBIDDEN_FEATURE_TOKENS: Final[tuple[str, ...]] = (
    "lat",
    "lon",
    "distance",
    "azimuth",
    "year",
    "magnitude",
    "depth",
    "station",
    "network",
    "sncl",
)

# 20-100 s is the single-force band the brief names. VLP extends below it; SP is the
# earthquake side. All in Hz, all inside the 0.005-5 Hz passband the windows were built with.
LP_BAND: Final = (0.01, 0.05)
VLP_BAND: Final = (0.005, 0.01)
SP_BAND: Final = (1.0, 5.0)
MID_BAND: Final = (0.05, 1.0)

ENVELOPE_SMOOTH_S: Final = 5.0
CENTROID_SEGMENT_S: Final = 30.0
EPSILON: Final = 1e-30

# The aggregates taken across a window's traces. Deliberately not the mean: with three to
# twelve traces of wildly different quality, one bad trace moves a mean and not a median.
AGGREGATES: Final[tuple[str, ...]] = ("med", "mad", "p90")


def _welch_power(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One-sided power spectrum of a Hann-windowed, demeaned trace, and its frequencies."""
    x = x - x.mean()
    spectrum = np.fft.rfft(x * np.hanning(x.size))
    freqs = np.fft.rfftfreq(x.size, d=1.0 / TARGET_SAMPLING_RATE_HZ)
    return freqs, np.abs(spectrum) ** 2


def _band_energy(freqs: np.ndarray, power: np.ndarray, band: tuple[float, float]) -> float:
    mask = (freqs >= band[0]) & (freqs < band[1])
    return float(power[mask].sum())


def _envelope(x: np.ndarray) -> np.ndarray:
    """Smoothed absolute-value envelope. Hilbert is avoided: it is far slower here and the
    5 s boxcar dominates the difference at the durations these features measure."""
    width = max(1, int(ENVELOPE_SMOOTH_S * TARGET_SAMPLING_RATE_HZ))
    kernel = np.ones(width) / width
    return np.convolve(np.abs(x), kernel, mode="same")


def _duration_above(envelope: np.ndarray, fraction: float) -> float:
    peak = float(envelope.max())
    if peak <= 0:
        return 0.0
    return float((envelope >= fraction * peak).sum()) / TARGET_SAMPLING_RATE_HZ


def _rise_time(envelope: np.ndarray) -> float:
    """Seconds from first crossing 10% of peak to first crossing 50%. The emergence measure."""
    peak = float(envelope.max())
    if peak <= 0:
        return 0.0
    low = np.flatnonzero(envelope >= 0.10 * peak)
    high = np.flatnonzero(envelope >= 0.50 * peak)
    if low.size == 0 or high.size == 0:
        return 0.0
    return float(max(0, high[0] - low[0])) / TARGET_SAMPLING_RATE_HZ


def _centroid_drift(x: np.ndarray) -> tuple[float, float]:
    """(mean centroid Hz, least-squares drift in Hz/s) over 30 s segments.

    The 'triangularity' the brief names: a slide's centroid rises into the acceleration phase
    and falls through deposition, so the drift over the window is large and signed, whereas an
    earthquake's centroid is near-constant after its coda begins.
    """
    step = int(CENTROID_SEGMENT_S * TARGET_SAMPLING_RATE_HZ)
    centroids, times = [], []
    for start in range(0, x.size - step + 1, step):
        segment = x[start : start + step]
        freqs, power = _welch_power(segment)
        total = float(power.sum())
        if total <= EPSILON:
            continue
        centroids.append(float((freqs * power).sum() / total))
        times.append((start + step / 2) / TARGET_SAMPLING_RATE_HZ)
    if len(centroids) < 3:
        return 0.0, 0.0
    slope = float(np.polyfit(np.asarray(times), np.asarray(centroids), 1)[0])
    return float(np.mean(centroids)), slope


def _trace_features(x: np.ndarray, prefix: str) -> dict[str, float]:
    """Twelve scalars describing one time series. `prefix` is `vert` or `horiz`."""
    freqs, power = _welch_power(x)
    total = float(power.sum()) + EPSILON
    lp = _band_energy(freqs, power, LP_BAND)
    vlp = _band_energy(freqs, power, VLP_BAND)
    sp = _band_energy(freqs, power, SP_BAND)
    mid = _band_energy(freqs, power, MID_BAND)
    centroid_mean, drift = _centroid_drift(x)
    envelope = _envelope(x)
    peak = float(envelope.max()) + EPSILON
    mean_env = float(envelope.mean()) + EPSILON
    spread = float(np.sqrt(max(0.0, ((freqs - centroid_mean) ** 2 * power).sum() / total)))
    return {
        f"{prefix}_log_lp_sp_ratio": float(np.log10((lp + EPSILON) / (sp + EPSILON))),
        f"{prefix}_log_vlp_sp_ratio": float(np.log10((vlp + EPSILON) / (sp + EPSILON))),
        f"{prefix}_log_lp_mid_ratio": float(np.log10((lp + EPSILON) / (mid + EPSILON))),
        f"{prefix}_energy_fraction_lp": float(lp / total),
        f"{prefix}_energy_fraction_sp": float(sp / total),
        f"{prefix}_spectral_centroid_hz": centroid_mean,
        f"{prefix}_centroid_drift_hz_per_s": drift,
        f"{prefix}_spectral_spread_hz": spread,
        f"{prefix}_envelope_duration_50pc_s": _duration_above(envelope, 0.5),
        f"{prefix}_envelope_duration_20pc_s": _duration_above(envelope, 0.2),
        f"{prefix}_envelope_rise_10_50_s": _rise_time(envelope),
        f"{prefix}_envelope_peak_over_mean": float(peak / mean_env),
    }


TRACE_FEATURE_NAMES: Final[tuple[str, ...]] = tuple(
    _trace_features(np.zeros(N_SAMPLES, dtype=np.float64), "vert")
)
_TRACE_STEMS: Final[tuple[str, ...]] = tuple(n.removeprefix("vert_") for n in TRACE_FEATURE_NAMES)

# Three-component descriptors, computed per receiver rather than per time series.
THREE_COMPONENT_NAMES: Final = ("log_hv_energy_ratio", "lp_rectilinearity")

PER_TRACE_STEMS: Final[tuple[str, ...]] = (
    tuple(f"vert_{s}" for s in _TRACE_STEMS)
    + tuple(f"horiz_{s}" for s in _TRACE_STEMS)
    + THREE_COMPONENT_NAMES
)

FEATURE_NAMES: Final[tuple[str, ...]] = (
    *(f"{stem}_{aggregate}" for stem in PER_TRACE_STEMS for aggregate in AGGREGATES),
    "lp_envelope_coherence",
)

N_FEATURES: Final = len(FEATURE_NAMES)


def _bandpass_fft(x: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    """Zero out everything outside `band` in the frequency domain. Used for polarisation."""
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / TARGET_SAMPLING_RATE_HZ)
    spectrum[(freqs < band[0]) | (freqs >= band[1])] = 0
    return np.fft.irfft(spectrum, n=x.size)


def _rectilinearity(z: np.ndarray, n: np.ndarray, e: np.ndarray) -> float:
    """1 - (l2 + l3) / (2 l1) on the long-period covariance eigenvalues; 1 is fully linear."""
    matrix = np.vstack([_bandpass_fft(c, LP_BAND) for c in (z, n, e)])
    matrix = matrix - matrix.mean(axis=1, keepdims=True)
    scale = float(np.abs(matrix).max())
    if scale <= 0:
        return 0.0
    eigenvalues = np.linalg.eigvalsh(np.cov(matrix / scale))
    l3, l2, l1 = np.sort(eigenvalues)
    if l1 <= EPSILON:
        return 0.0
    return float(np.clip(1.0 - (l2 + l3) / (2.0 * l1), 0.0, 1.0))


def _aggregate(values: list[float], stem: str) -> dict[str, float]:
    """Median, median absolute deviation and 90th percentile across a window's traces."""
    if not values:
        return dict.fromkeys((f"{stem}_{a}" for a in AGGREGATES), 0.0)
    array = np.asarray(values, dtype=np.float64)
    median = float(np.median(array))
    return {
        f"{stem}_med": median,
        f"{stem}_mad": float(np.median(np.abs(array - median))),
        f"{stem}_p90": float(np.percentile(array, 90)),
    }


def _coherence(envelopes: list[np.ndarray]) -> float:
    """Mean pairwise Pearson correlation of normalised long-period envelopes."""
    if len(envelopes) < 2:
        return 0.0
    stacked = np.vstack(envelopes)
    stacked = stacked - stacked.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(stacked, axis=1, keepdims=True)
    if float(norms.min()) <= EPSILON:
        return 0.0
    matrix = (stacked / norms) @ (stacked / norms).T
    upper = matrix[np.triu_indices(matrix.shape[0], k=1)]
    return float(np.mean(upper))


def compute_features(waveform: np.ndarray, valid: np.ndarray) -> dict[str, float]:
    """The feature vector for one window.

    `waveform` is (n_slots, 3, N_SAMPLES) velocity; `valid` is (n_slots, 3). Only slots whose
    vertical component is valid contribute, because every per-receiver descriptor needs it;
    horizontals are used when both are valid and skipped otherwise. A window with no valid
    slot yields all-zero features rather than NaNs, so a downstream model never silently drops
    a row it should have refused.
    """
    per_trace: dict[str, list[float]] = {stem: [] for stem in PER_TRACE_STEMS}
    envelopes: list[np.ndarray] = []

    for slot in range(waveform.shape[0]):
        if not bool(valid[slot, 0]):
            continue
        vertical = np.asarray(waveform[slot, 0], dtype=np.float64)
        if not np.any(vertical) or not np.all(np.isfinite(vertical)):
            continue
        for name, value in _trace_features(vertical, "vert").items():
            per_trace[name].append(value)

        north = np.asarray(waveform[slot, 1], dtype=np.float64)
        east = np.asarray(waveform[slot, 2], dtype=np.float64)
        has_horizontals = (
            bool(valid[slot, 1])
            and bool(valid[slot, 2])
            and bool(np.all(np.isfinite(north)) and np.all(np.isfinite(east)))
        )
        if has_horizontals:
            horizontal = np.hypot(north, east)
            for name, value in _trace_features(horizontal, "horiz").items():
                per_trace[name].append(value)
            vertical_energy = float(np.sum(vertical**2)) + EPSILON
            horizontal_energy = float(np.sum(north**2) + np.sum(east**2)) + EPSILON
            per_trace["log_hv_energy_ratio"].append(
                float(np.log10(horizontal_energy / vertical_energy))
            )
            per_trace["lp_rectilinearity"].append(_rectilinearity(vertical, north, east))

        envelope = _envelope(_bandpass_fft(vertical, LP_BAND))
        peak = float(envelope.max())
        if peak > 0:
            envelopes.append(envelope / peak)

    features: dict[str, float] = {}
    for stem in PER_TRACE_STEMS:
        features.update(_aggregate(per_trace[stem], stem))
    features["lp_envelope_coherence"] = _coherence(envelopes)
    return {name: features[name] for name in FEATURE_NAMES}


def feature_matrix(waveforms: np.ndarray, valids: np.ndarray) -> np.ndarray:
    """(n_windows, N_FEATURES) float64, rows in the order the arrays were given."""
    out = np.zeros((waveforms.shape[0], N_FEATURES), dtype=np.float64)
    for row in range(waveforms.shape[0]):
        values = compute_features(waveforms[row], valids[row])
        out[row] = [values[name] for name in FEATURE_NAMES]
    return out


def audit_feature_names(names: tuple[str, ...] = FEATURE_NAMES) -> list[str]:
    """Feature names containing a forbidden token. Empty is the only acceptable result."""
    return [
        name for name in names if any(token in name.lower() for token in FORBIDDEN_FEATURE_TOKENS)
    ]
