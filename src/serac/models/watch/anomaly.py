"""Anomaly model v0: exactly what `reports/watch/PREREGISTRATION.md` says, and nothing else.

Every constant here is a copy of a pre-registered value; the module refuses to hold any other
kind of number. Two properties are load-bearing and are enforced by tests rather than by
convention:

**Causality.** Every statistic at time `T` is computed from samples with `t <= T` only. There
is no smoother that peeks forward, no centred window, no global normalisation over the whole
record. `test_anomaly.py` proves it mechanically: it appends future samples, truncates them
again, and asserts the coefficients are bit-identical.

**No hindsight.** Nothing in this module — or in `tiers.py`'s thresholds — may read an event
record, a failure date, or a source-zone outline. The failed unit is identified only in the
reporting layer, after the scoring is done. `test_no_hindsight.py` asserts this by inspecting
the import graph and the module source.

The score is a robust z-score. **It is not a probability and it is never a failure date.**
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]

# -- pre-registered constants (PREREGISTRATION.md section 2) -------------------------------

SENS_FLOOR: Final[float] = 0.30
TRAILING_WINDOW_DAYS: Final[float] = 180.0
MIN_SAMPLES: Final[int] = 24
MIN_HISTORY_STEPS: Final[int] = 8
MIN_COHERENCE: Final[float] = 0.30
HARMONIC_ORDERS: Final[int] = 2
ELEVATED_THRESHOLD: Final[float] = 2.0
WATCH_THRESHOLD: Final[float] = 3.0
SAMPLE_WINDOW_DAYS: Final[float] = 730.0
YEAR_DAYS: Final[float] = 365.25
MAD_SCALE: Final[float] = 1.4826
IQR_SCALE: Final[float] = 1.349


class Tier(StrEnum):
    """The ordinal watch tier. Deliberately has no member that implies a time or a probability."""

    quiet = "quiet"
    elevated = "elevated"
    watch = "watch"
    insufficient_data = "insufficient_data"


TIER_ORDER: Final[dict[Tier, int]] = {
    Tier.insufficient_data: -1,
    Tier.quiet: 0,
    Tier.elevated: 1,
    Tier.watch: 2,
}


class InsufficientReason(StrEnum):
    """Why a unit could not be scored. A unit that cannot be measured is never `quiet`."""

    outside_footprint = "outside_footprint"
    low_los_sensitivity = "low_los_sensitivity"
    too_few_samples = "too_few_samples"
    low_coherence = "low_coherence"
    too_little_history = "too_little_history"


@dataclass(frozen=True)
class UnitScore:
    """One unit at one evaluation step."""

    unit_id: str
    tier: Tier
    score: float
    velocity_mm_yr: float | None
    acceleration_mm_yr2: float | None
    z_velocity: float | None
    z_acceleration: float | None
    n_samples: int
    median_coherence: float | None
    reason: InsufficientReason | None = None


def robust_scale(values: FloatArray) -> float:
    """MAD-based scale with the pre-registered IQR fallback; 0.0 when the sample has no spread."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    mad = float(np.median(np.abs(finite - np.median(finite)))) * MAD_SCALE
    if mad > 0:
        return mad
    q75, q25 = np.percentile(finite, [75, 25])
    iqr = float(q75 - q25) / IQR_SCALE
    return iqr if iqr > 0 else 0.0


def robust_z(value: float, population: FloatArray) -> float:
    """`(value - median) / scale`, or 0.0 when the population cannot rank anything."""
    finite = population[np.isfinite(population)]
    if finite.size == 0 or not np.isfinite(value):
        return 0.0
    scale = robust_scale(finite)
    if scale <= 0:
        return 0.0
    return float((value - float(np.median(finite))) / scale)


def harmonic_design(t_days: FloatArray, orders: int = HARMONIC_ORDERS) -> FloatArray:
    """`[1, t, cos, sin, cos2, sin2]` — the pre-registered decomposition's design matrix.

    Harmonic regression rather than STL because the sampling is irregular (12-day nominal with
    gaps of up to 48 days) and STL needs a regular grid.
    """
    columns = [np.ones_like(t_days), t_days]
    for k in range(1, orders + 1):
        omega = 2.0 * np.pi * k / YEAR_DAYS
        columns.append(np.cos(omega * t_days))
        columns.append(np.sin(omega * t_days))
    return np.column_stack(columns)


def deseasonalise(
    t_days: FloatArray, values: FloatArray, orders: int = HARMONIC_ORDERS
) -> FloatArray:
    """Remove the fitted seasonal terms, keeping the trend. Fitted on the given samples only."""
    finite = np.isfinite(values) & np.isfinite(t_days)
    out = np.full_like(values, np.nan)
    if int(finite.sum()) < 2 * orders + 3:
        out[finite] = values[finite]
        return out
    design = harmonic_design(t_days[finite], orders)
    coefficients, *_ = np.linalg.lstsq(design, values[finite], rcond=None)
    seasonal = design[:, 2:] @ coefficients[2:]
    out[finite] = values[finite] - seasonal
    return out


def trailing_slope(
    t_days: FloatArray, values: FloatArray, *, end: float, window_days: float = TRAILING_WINDOW_DAYS
) -> float | None:
    """OLS slope over `(end - window, end]`, in units per year. None when under-determined."""
    inside = np.isfinite(values) & (t_days > end - window_days) & (t_days <= end)
    if int(inside.sum()) < 3:
        return None
    t = t_days[inside]
    if float(t.max() - t.min()) <= 0:
        return None
    design = np.column_stack([np.ones_like(t), t])
    coefficients, *_ = np.linalg.lstsq(design, values[inside], rcond=None)
    return float(coefficients[1] * YEAR_DAYS)


@dataclass(frozen=True)
class UnitSeries:
    """One slope unit's causal inputs. `t_days` is days since a fixed epoch, strictly sorted."""

    unit_id: str
    t_days: FloatArray
    los_mm: FloatArray
    coherence: FloatArray
    los_sensitivity_signed: float
    inside_footprint: bool = True

    def upto(self, end: float) -> UnitSeries:
        """The same series truncated to `t <= end`. This is the only way the model sees time."""
        keep = self.t_days <= end
        return UnitSeries(
            unit_id=self.unit_id,
            t_days=self.t_days[keep],
            los_mm=self.los_mm[keep],
            coherence=self.coherence[keep],
            los_sensitivity_signed=self.los_sensitivity_signed,
            inside_footprint=self.inside_footprint,
        )

    def downslope_mm(self) -> FloatArray:
        """LOS displacement converted to a downslope-equivalent, per PREREGISTRATION section 1."""
        sensitivity = self.los_sensitivity_signed
        if abs(sensitivity) < SENS_FLOOR:
            sensitivity = float(np.sign(sensitivity) or 1.0) * SENS_FLOOR
        return self.los_mm / sensitivity


@dataclass(frozen=True)
class UnitState:
    """Velocity and acceleration for one unit at one step, or the reason there are none."""

    unit_id: str
    velocity: float | None
    acceleration: float | None
    n_samples: int
    median_coherence: float | None
    reason: InsufficientReason | None


def unit_state(series: UnitSeries, end: float) -> UnitState:
    """Everything section 4 asks for, computed from samples with `t <= end` only."""
    if not series.inside_footprint:
        return UnitState(series.unit_id, None, None, 0, None, InsufficientReason.outside_footprint)
    if abs(series.los_sensitivity_signed) < SENS_FLOOR:
        return UnitState(
            series.unit_id, None, None, 0, None, InsufficientReason.low_los_sensitivity
        )
    causal = series.upto(end)
    recent = causal.t_days > end - SAMPLE_WINDOW_DAYS
    n_samples = int((recent & np.isfinite(causal.los_mm)).sum())
    if n_samples < MIN_SAMPLES:
        return UnitState(
            series.unit_id, None, None, n_samples, None, InsufficientReason.too_few_samples
        )
    trailing = causal.t_days > end - TRAILING_WINDOW_DAYS
    coherence_window = causal.coherence[trailing]
    coherence_window = coherence_window[np.isfinite(coherence_window)]
    median_coherence = float(np.median(coherence_window)) if coherence_window.size else None
    if median_coherence is None or median_coherence < MIN_COHERENCE:
        return UnitState(
            series.unit_id,
            None,
            None,
            n_samples,
            median_coherence,
            InsufficientReason.low_coherence,
        )
    deseasonalised = deseasonalise(causal.t_days, causal.downslope_mm())
    velocity = trailing_slope(causal.t_days, deseasonalised, end=end)
    previous = trailing_slope(causal.t_days, deseasonalised, end=end - TRAILING_WINDOW_DAYS)
    acceleration = (
        None
        if velocity is None or previous is None
        else (velocity - previous) / (TRAILING_WINDOW_DAYS / YEAR_DAYS)
    )
    return UnitState(series.unit_id, velocity, acceleration, n_samples, median_coherence, None)


def score_step(
    states: dict[str, UnitState],
    history: dict[str, list[UnitState]],
) -> dict[str, UnitScore]:
    """Turn one step's states into tiers, using the pre-registered `max(min(z_t, z_s))` rule.

    `history` holds the states of *earlier* steps only; the caller is responsible for that and
    the causality test checks it end to end.
    """
    measurable = {u: s for u, s in states.items() if s.reason is None}
    velocities = np.array(
        [s.velocity for s in measurable.values() if s.velocity is not None], dtype=np.float64
    )
    accelerations = np.array(
        [s.acceleration for s in measurable.values() if s.acceleration is not None],
        dtype=np.float64,
    )

    out: dict[str, UnitScore] = {}
    for unit_id, state in states.items():
        if state.reason is not None:
            out[unit_id] = UnitScore(
                unit_id=unit_id,
                tier=Tier.insufficient_data,
                score=float("nan"),
                velocity_mm_yr=None,
                acceleration_mm_yr2=None,
                z_velocity=None,
                z_acceleration=None,
                n_samples=state.n_samples,
                median_coherence=state.median_coherence,
                reason=state.reason,
            )
            continue
        past = [s for s in history.get(unit_id, []) if s.reason is None]
        if len(past) < MIN_HISTORY_STEPS:
            out[unit_id] = UnitScore(
                unit_id=unit_id,
                tier=Tier.insufficient_data,
                score=float("nan"),
                velocity_mm_yr=state.velocity,
                acceleration_mm_yr2=state.acceleration,
                z_velocity=None,
                z_acceleration=None,
                n_samples=state.n_samples,
                median_coherence=state.median_coherence,
                reason=InsufficientReason.too_little_history,
            )
            continue
        z_v = _combined_z(state.velocity, [s.velocity for s in past], velocities)
        z_a = _combined_z(state.acceleration, [s.acceleration for s in past], accelerations)
        score = max(z_v, z_a)
        out[unit_id] = UnitScore(
            unit_id=unit_id,
            tier=tier_for(score),
            score=score,
            velocity_mm_yr=state.velocity,
            acceleration_mm_yr2=state.acceleration,
            z_velocity=z_v,
            z_acceleration=z_a,
            n_samples=state.n_samples,
            median_coherence=state.median_coherence,
        )
    return out


def _combined_z(value: float | None, own_history: list[float | None], peers: FloatArray) -> float:
    """`min(z_temporal, z_spatial)` — see PREREGISTRATION section 4 for why the minimum."""
    if value is None:
        return 0.0
    history = np.array([v for v in own_history if v is not None], dtype=np.float64)
    if history.size < MIN_HISTORY_STEPS:
        return 0.0
    z_temporal = robust_z(value, history)
    z_spatial = robust_z(value, peers)
    return min(z_temporal, z_spatial)


def tier_for(score: float) -> Tier:
    """The pre-registered thresholds; the only place a score becomes a tier."""
    if not np.isfinite(score):
        return Tier.insufficient_data
    if score >= WATCH_THRESHOLD:
        return Tier.watch
    if score >= ELEVATED_THRESHOLD:
        return Tier.elevated
    return Tier.quiet


def walk_forward(
    series_by_unit: dict[str, UnitSeries], steps: list[float]
) -> list[dict[str, UnitScore]]:
    """Score every unit at every step, feeding each step only what preceded it.

    The single place the walk-forward loop lives, so there is exactly one implementation for
    the causality test to check.
    """
    history: dict[str, list[UnitState]] = {u: [] for u in series_by_unit}
    out: list[dict[str, UnitScore]] = []
    for end in sorted(steps):
        states = {u: unit_state(s, end) for u, s in series_by_unit.items()}
        out.append(score_step(states, history))
        for unit_id, state in states.items():
            history[unit_id].append(state)
    return out
