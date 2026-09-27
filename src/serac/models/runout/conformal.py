"""Making the surrogate's 5-95 % interval mean 90 %, by calibrating it on held-out members.

The surrogate publishes quantile heads, and a quantile head is not a calibrated interval. Its
5th and 95th percentiles are whatever the pinball loss settled on, and on the frozen ensemble the
arrival-time interval came out **too narrow**: measured coverage 0.7939 against the brief's
[0.85, 0.95] gate, which `validate-runout` reports as a failure (Gap 42). Training longer does not
fix that. The interval is mis-calibrated, not under-trained, and the remedy is to measure the
miscalibration on data the model did not fit and correct for it.

**Conformalized quantile regression** (Romano, Patterson & Candès 2019) is the standard
construction. On a calibration split, score each point by how far outside its own predicted
interval the truth fell::

    E_i = max(lower_i - y_i, y_i - upper_i)

`E_i` is negative when the truth sat comfortably inside. Take the conservative empirical quantile
`Q` of those scores at the nominal level and publish `[lower - Q, upper + Q]`. The guarantee is
finite-sample, distribution-free and two-sided: coverage is at least the nominal level, and when
the model *over*-covers `Q` comes out negative and the interval narrows rather than staying
needlessly wide. Nothing about the model is retrained; one number per target is stored beside it.

Two variants, because arrival-time error is not homoscedastic:

* ``ADDITIVE`` shifts both ends by the same number of seconds. Simple, and it flattens the
  model's own sense of which predictions are uncertain.
* ``SCALED`` divides the score by the predicted interval width, so the correction is proportional
  to the width the model already produced. This keeps a wide prediction wide and a confident one
  narrow, which is the behaviour worth preserving in a surrogate whose whole job is to say when
  it does not know. It is the default.

**Three limits, none of them small.**

*Exchangeability is the assumption*, and it is doing real work. The calibration split is disjoint
by ``run_id``, so members of one simulation cannot straddle the split, but members of a single
frozen ensemble are exchangeable only if the parameter draws behind them were. They were drawn to
a design, not at random, and that is a caveat this docstring will not bury.

*The guarantee is marginal, not conditional.* Coverage holds on average over the calibration
distribution, not per transect. `validate-runout` scores per-transect arrival MAE separately, and
a marginal correction does not promise anything there.

*Arrival time cannot be negative*, so the lower end is clamped at zero. Clamping only ever removes
interval, so realised coverage can sit slightly below nominal where predictions crowd zero, and
:func:`coverage` measures what was realised rather than what was promised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import numpy.typing as npt

CONFORMAL_VERSION = "0.1.0"
DEFAULT_LEVEL = 0.90
"""The 5-95 % interval the brief gates, expressed as the coverage it should deliver."""


class ConformalMode(StrEnum):
    """How the correction is applied to an interval."""

    ADDITIVE = "additive"
    SCALED = "scaled"


@dataclass(frozen=True, slots=True)
class ConformalCalibration:
    """One target's correction, and what it did on the split it was fitted on."""

    target: str
    mode: ConformalMode
    level: float
    correction: float
    n_calibration: int
    coverage_before: float
    coverage_after: float
    non_negative: bool
    version: str = CONFORMAL_VERSION

    @property
    def widens(self) -> bool:
        """True when the model was under-covering and the interval had to grow."""
        return self.correction > 0.0

    def render(self) -> str:
        direction = "widened" if self.widens else "narrowed"
        unit = "x width" if self.mode is ConformalMode.SCALED else "units"
        return (
            f"{self.target}: coverage {self.coverage_before:.4f} -> {self.coverage_after:.4f} "
            f"against a nominal {self.level:.2f}; {direction} by {abs(self.correction):.4g} "
            f"{unit} ({self.mode.value}, {self.n_calibration} calibration point(s))"
        )


def _as_arrays(
    lower: npt.ArrayLike, upper: npt.ArrayLike, truth: npt.ArrayLike
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    lo = np.asarray(lower, dtype=np.float64).ravel()
    hi = np.asarray(upper, dtype=np.float64).ravel()
    y = np.asarray(truth, dtype=np.float64).ravel()
    if not (lo.shape == hi.shape == y.shape):
        msg = (
            f"lower, upper and truth must be the same length: got {lo.shape}, {hi.shape}, {y.shape}"
        )
        raise ValueError(msg)
    if lo.size == 0:
        msg = "no calibration points"
        raise ValueError(msg)
    if np.any(hi < lo):
        msg = "upper quantile falls below lower quantile; the heads are crossing"
        raise ValueError(msg)
    finite = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(y)
    if not finite.all():
        lo, hi, y = lo[finite], hi[finite], y[finite]
    if lo.size == 0:
        msg = "every calibration point was non-finite"
        raise ValueError(msg)
    return lo, hi, y


def conformity_scores(
    lower: npt.ArrayLike,
    upper: npt.ArrayLike,
    truth: npt.ArrayLike,
    *,
    mode: ConformalMode = ConformalMode.SCALED,
    width_floor: float = 1e-9,
) -> npt.NDArray[np.float64]:
    """``max(lower - y, y - upper)``, divided by the interval width under ``SCALED``.

    Negative where the truth fell inside the interval, which is what lets the correction narrow
    an over-confident-in-the-other-direction model rather than only ever widening.
    """
    lo, hi, y = _as_arrays(lower, upper, truth)
    raw = np.maximum(lo - y, y - hi)
    if mode is ConformalMode.ADDITIVE:
        return raw
    width = np.maximum(hi - lo, width_floor)
    return raw / width


def conservative_quantile(scores: npt.ArrayLike, level: float) -> float:
    """The ``ceil((n + 1) * level) / n`` empirical quantile the CQR guarantee is stated for.

    Not ``numpy.percentile``: the finite-sample guarantee needs the ``(n+1)`` correction, and on
    the small calibration splits this surrogate has — 34 held-out members — the difference between
    the two is not academic. When the level demands a rank beyond the sample, the largest score is
    the best available answer and the interval it produces is the widest the data can justify.
    """
    s = np.sort(np.asarray(scores, dtype=np.float64).ravel())
    n = s.size
    if n == 0:
        msg = "no scores to take a quantile of"
        raise ValueError(msg)
    if not 0.0 < level < 1.0:
        msg = f"level must lie in (0, 1), got {level}"
        raise ValueError(msg)
    rank = math.ceil((n + 1) * level)
    if rank > n:
        return float(s[-1])
    return float(s[rank - 1])


def apply_correction(
    lower: npt.ArrayLike,
    upper: npt.ArrayLike,
    correction: float,
    *,
    mode: ConformalMode = ConformalMode.SCALED,
    non_negative: bool = False,
    width_floor: float = 1e-9,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Widen (or narrow) an interval by ``correction``; clamp the lower end if the target cannot
    be negative."""
    lo = np.asarray(lower, dtype=np.float64)
    hi = np.asarray(upper, dtype=np.float64)
    if mode is ConformalMode.ADDITIVE:
        delta = np.full_like(lo, float(correction))
    else:
        delta = float(correction) * np.maximum(hi - lo, width_floor)
    out_lo = lo - delta
    out_hi = hi + delta
    # A narrowing correction can cross the ends over each other; collapse to the midpoint rather
    # than publish an inverted interval.
    crossed = out_hi < out_lo
    if np.any(crossed):
        midpoint = 0.5 * (out_lo + out_hi)
        out_lo = np.where(crossed, midpoint, out_lo)
        out_hi = np.where(crossed, midpoint, out_hi)
    if non_negative:
        out_lo = np.maximum(out_lo, 0.0)
        out_hi = np.maximum(out_hi, 0.0)
    return out_lo, out_hi


def coverage(lower: npt.ArrayLike, upper: npt.ArrayLike, truth: npt.ArrayLike) -> float:
    """Fraction of truths inside ``[lower, upper]``, inclusive."""
    lo, hi, y = _as_arrays(lower, upper, truth)
    return float(np.mean((y >= lo) & (y <= hi)))


def fit(
    lower: npt.ArrayLike,
    upper: npt.ArrayLike,
    truth: npt.ArrayLike,
    *,
    target: str,
    level: float = DEFAULT_LEVEL,
    mode: ConformalMode = ConformalMode.SCALED,
    non_negative: bool = False,
) -> ConformalCalibration:
    """Fit the correction on a calibration split and measure what it achieved there.

    ``coverage_after`` is measured on the same points the correction was fitted on, so it is not
    an independent estimate of anything — it is a check that the arithmetic did what it claims.
    The number that matters is coverage on the *test* split, which the training report measures
    separately and which this function deliberately does not touch.
    """
    lo, hi, y = _as_arrays(lower, upper, truth)
    before = coverage(lo, hi, y)
    scores = conformity_scores(lo, hi, y, mode=mode)
    correction = conservative_quantile(scores, level)
    new_lo, new_hi = apply_correction(lo, hi, correction, mode=mode, non_negative=non_negative)
    return ConformalCalibration(
        target=target,
        mode=mode,
        level=level,
        correction=correction,
        n_calibration=int(lo.size),
        coverage_before=before,
        coverage_after=coverage(new_lo, new_hi, y),
        non_negative=non_negative,
    )
