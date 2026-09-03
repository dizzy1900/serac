"""Cascade rules **v0**: damming index, a parametric breach hydrograph, secondary surge arrival.

Read this before using any number out of this module
----------------------------------------------------
The grid is 30 m. The Bhote Koshi gorge is **under 60 m wide in places**, so the channel that
these rules measure spans fewer than two cells there. Superelevation, run-up on valley walls and
the actual blocking geometry are all unresolved. What follows is a **dimensionless index and a
set of parametric relations**, not an engineering estimate of a landslide dam:

* the damming index compares modelled deposit depth against a channel depth read off the same
  30 m DEM, so both sides of the ratio carry the same resolution error;
* the breach hydrograph is a parametric triangular wave whose peak comes from a published-form
  regression on dam height and impounded volume -- it is not routed, not a physical breach
  model, and has no sediment;
* the secondary-surge arrival is the breach peak translated downstream at a wave celerity, not
  a solved flood routing.

Every number this module emits carries its own assumption string, and `DAMMING_V0_LABEL` is
written next to every one of them. `probability` is reported as a `Range` spanning the index's
own uncertainty and is explicitly **not** a calibrated probability: there is no inventory of
landslide dams on this corridor to calibrate against, and one event is not a sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from serac.models.runout.params import RESOLUTION_LIMITATION

F64 = NDArray[np.float64]

DAMMING_V0_LABEL = "cascade rules v0"

DAMMING_INDEX_ASSUMPTION = (
    "Damming index v0 = deposit depth / channel depth at a constriction, both measured on the "
    "same 30 m DEM. It is a dimensionless indicator, not a probability derived from data: no "
    "inventory of landslide dams exists for this corridor. Channel width defaults to 60 m -- a "
    "stated value, not a survey -- wherever no measured cross-section is supplied."
)
BREACH_ASSUMPTION = (
    "Breach hydrograph v0 is a parametric triangular wave: peak discharge from a published-form "
    "regression Qp = k * (V_lake * H_dam)^a with k and a stated below, rise time one third of "
    "the total, no routing, no sediment, no progressive erosion. It is a shape, not a breach "
    "model."
)
SURGE_ASSUMPTION = (
    "Secondary-surge arrival v0 translates the breach peak downstream at a constant wave "
    "celerity derived from the channel depth. It is not a solved flood routing and ignores "
    "attenuation, tributary inflow and channel storage."
)

BREACH_K = 0.0026
BREACH_A = 0.79
"""Froehlich-form peak-discharge regression exponents. Published *form*; the constants here are
the widely-quoted values for `Qp` in m3/s from `V_lake` (m3) and `H_dam` (m). They are used as a
shape parameter, not as a validated predictor for this corridor."""

MIN_CHANNEL_DEPTH_M = 5.0
"""Below this the 30 m DEM is not resolving a channel at all and the index is not reported."""

DEFAULT_CHANNEL_WIDTH_M = 60.0
"""Stated default where no measured width is available -- two 30 m cells.

There is no surveyed cross-section for this corridor, and the DEM cannot supply one at 30 m in a
gorge narrower than two cells. Using NaN here instead propagated straight into a `Range` and was
caught only by pydantic refusing to build one; a stated default that travels with an assumption
string is the honest form."""


@dataclass(frozen=True)
class Constriction:
    """A candidate damming site on the corridor profile."""

    chainage_m: float
    channel_depth_m: float
    channel_width_m: float
    bed_elevation_m: float


@dataclass(frozen=True)
class DammingIndicator:
    """The v0 damming assessment at one constriction."""

    chainage_m: float
    deposit_depth_m: float
    channel_depth_m: float
    channel_width_m: float
    index: float
    index_low: float
    index_high: float
    dam_height_m: float
    lake_volume_m3: float

    @property
    def assumptions(self) -> list[str]:
        return [DAMMING_INDEX_ASSUMPTION, RESOLUTION_LIMITATION]

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": DAMMING_V0_LABEL,
            "chainage_m": round(self.chainage_m, 1),
            "deposit_depth_m": round(self.deposit_depth_m, 3),
            "channel_depth_m": round(self.channel_depth_m, 3),
            "channel_width_m": round(self.channel_width_m, 1),
            "damming_index": round(self.index, 4),
            "damming_index_low": round(self.index_low, 4),
            "damming_index_high": round(self.index_high, 4),
            "dam_height_m": round(self.dam_height_m, 3),
            "lake_volume_m3": round(self.lake_volume_m3, 1),
            "assumptions": self.assumptions,
        }


def find_constrictions(
    chainage_m: F64,
    bed_min_m: F64,
    *,
    corridor_width_m: F64 | None = None,
    n_sites: int = 12,
    min_separation_m: float = 2000.0,
) -> list[Constriction]:
    """The narrowest / most confined chainage bins, as candidate damming sites.

    Channel depth is the local relief between the thalweg and the surrounding valley floor,
    read from the binned bed profile: the difference between a long-window running maximum and
    the bin's own minimum. At 30 m in a sub-60 m gorge that is a proxy for confinement, not a
    surveyed cross-section, which is why `MIN_CHANNEL_DEPTH_M` drops sites the DEM cannot see.
    """
    finite = np.isfinite(bed_min_m)
    if finite.sum() < 5:
        return []
    z = np.where(finite, bed_min_m, np.nanmax(bed_min_m))
    window = max(3, round(1500.0 / max(chainage_m[1] - chainage_m[0], 1.0)))
    padded = np.pad(z, window, mode="edge")
    shoulder = np.array(
        [padded[i : i + 2 * window + 1].max() for i in range(len(z))], dtype=np.float64
    )
    depth = shoulder - z
    width = (
        corridor_width_m
        if corridor_width_m is not None
        else np.full_like(depth, DEFAULT_CHANNEL_WIDTH_M)
    )
    order = np.argsort(depth)[::-1]
    chosen: list[Constriction] = []
    for i in order:
        if depth[i] < MIN_CHANNEL_DEPTH_M or not finite[i]:
            continue
        if any(abs(chainage_m[i] - c.chainage_m) < min_separation_m for c in chosen):
            continue
        chosen.append(
            Constriction(
                chainage_m=float(chainage_m[i]),
                channel_depth_m=float(depth[i]),
                channel_width_m=float(width[i]),
                bed_elevation_m=float(z[i]),
            )
        )
        if len(chosen) >= n_sites:
            break
    return sorted(chosen, key=lambda c: c.chainage_m)


def damming_index(
    constriction: Constriction, deposit_depth_m: float, *, uncertainty: float = 0.5
) -> DammingIndicator:
    """Deposit depth against channel depth, with a stated +/- band.

    `uncertainty` is a **stated** fractional band on the index, not a fitted one: it exists so
    that a downstream `Range` cannot be built as a point value, and its width reflects that
    both the deposit and the channel depth come from the same 30 m surface.
    """
    ratio = deposit_depth_m / max(constriction.channel_depth_m, 1e-6)
    dam_height = min(deposit_depth_m, constriction.channel_depth_m)
    # a wedge-shaped impoundment behind a dam of that height, over the local channel slope
    lake_volume = 0.5 * dam_height * dam_height * max(constriction.channel_width_m, 30.0) / 0.01
    return DammingIndicator(
        chainage_m=constriction.chainage_m,
        deposit_depth_m=deposit_depth_m,
        channel_depth_m=constriction.channel_depth_m,
        channel_width_m=constriction.channel_width_m,
        index=ratio,
        index_low=ratio * (1.0 - uncertainty),
        index_high=ratio * (1.0 + uncertainty),
        dam_height_m=dam_height,
        lake_volume_m3=lake_volume,
    )


def index_to_probability(index: float) -> float:
    """Map the damming index onto [0, 1] with a stated logistic. **Not calibrated.**

    The midpoint sits at index 1 (deposit as deep as the channel) and the scale is 0.4. Both
    are chosen so the mapping is monotone and saturating; neither is fitted to anything, and
    the resulting number must never be described as a probability of damming derived from data.
    """
    return float(1.0 / (1.0 + math.exp(-(index - 1.0) / 0.4)))


@dataclass(frozen=True)
class BreachHydrograph:
    """A parametric triangular breach wave. v0: a shape, not a breach model."""

    peak_discharge_m3s: float
    rise_time_s: float
    total_time_s: float
    lake_volume_m3: float
    dam_height_m: float

    def discharge_at(self, t_s: float) -> float:
        if t_s <= 0.0 or t_s >= self.total_time_s:
            return 0.0
        if t_s <= self.rise_time_s:
            return self.peak_discharge_m3s * t_s / self.rise_time_s
        fall = self.total_time_s - self.rise_time_s
        return self.peak_discharge_m3s * (self.total_time_s - t_s) / fall

    @property
    def assumptions(self) -> list[str]:
        return [BREACH_ASSUMPTION, RESOLUTION_LIMITATION]

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": DAMMING_V0_LABEL,
            "peak_discharge_m3s": round(self.peak_discharge_m3s, 1),
            "rise_time_s": round(self.rise_time_s, 1),
            "total_time_s": round(self.total_time_s, 1),
            "lake_volume_m3": round(self.lake_volume_m3, 1),
            "dam_height_m": round(self.dam_height_m, 2),
            "assumptions": self.assumptions,
        }


def breach_hydrograph(indicator: DammingIndicator) -> BreachHydrograph:
    """Triangular wave: area equals the impounded volume, peak from `BREACH_K`."""
    volume = max(indicator.lake_volume_m3, 1.0)
    height = max(indicator.dam_height_m, 0.1)
    peak = BREACH_K * (volume * height) ** BREACH_A
    # the triangle's area must be the lake volume: 0.5 * Qp * T = V
    total = 2.0 * volume / max(peak, 1e-6)
    return BreachHydrograph(
        peak_discharge_m3s=peak,
        rise_time_s=total / 3.0,
        total_time_s=total,
        lake_volume_m3=volume,
        dam_height_m=height,
    )


@dataclass(frozen=True)
class SecondarySurge:
    """When a breach wave would reach a downstream chainage, and how big it would be."""

    from_chainage_m: float
    to_chainage_m: float
    travel_time_s: float
    celerity_m_s: float
    peak_discharge_m3s: float

    @property
    def assumptions(self) -> list[str]:
        return [SURGE_ASSUMPTION, BREACH_ASSUMPTION, RESOLUTION_LIMITATION]

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": DAMMING_V0_LABEL,
            "from_chainage_m": round(self.from_chainage_m, 1),
            "to_chainage_m": round(self.to_chainage_m, 1),
            "travel_time_s": round(self.travel_time_s, 1),
            "celerity_m_s": round(self.celerity_m_s, 3),
            "peak_discharge_m3s": round(self.peak_discharge_m3s, 1),
            "assumptions": self.assumptions,
        }


def secondary_surge(
    indicator: DammingIndicator,
    hydrograph: BreachHydrograph,
    to_chainage_m: float,
    *,
    gravity: float = 9.80665,
) -> SecondarySurge | None:
    """Translate the breach peak downstream at `sqrt(g h)` plus the mean flow speed.

    Returns None when the target is upstream of the dam. No attenuation: a real routing would
    reduce the peak over 50 km, so this over-states the surge downstream and that is stated
    rather than fudged with an unjustified decay constant.
    """
    distance = to_chainage_m - indicator.chainage_m
    if distance <= 0.0:
        return None
    depth = max(indicator.dam_height_m, 1.0)
    celerity = math.sqrt(gravity * depth) * 1.5
    return SecondarySurge(
        from_chainage_m=indicator.chainage_m,
        to_chainage_m=to_chainage_m,
        travel_time_s=distance / celerity,
        celerity_m_s=celerity,
        peak_discharge_m3s=hydrograph.peak_discharge_m3s,
    )


def assess(
    chainage_m: F64,
    bed_min_m: F64,
    deposit_depth_m: F64,
    *,
    n_sites: int = 12,
) -> list[dict[str, Any]]:
    """Full v0 cascade assessment along one member's corridor profile."""
    out: list[dict[str, Any]] = []
    for constriction in find_constrictions(chainage_m, bed_min_m, n_sites=n_sites):
        idx = int(np.argmin(np.abs(chainage_m - constriction.chainage_m)))
        deposit = float(deposit_depth_m[idx])
        if deposit <= 0.0:
            continue
        indicator = damming_index(constriction, deposit)
        hydrograph = breach_hydrograph(indicator)
        out.append(
            {
                **indicator.as_dict(),
                "probability_uncalibrated": round(index_to_probability(indicator.index), 4),
                "probability_low": round(index_to_probability(indicator.index_low), 4),
                "probability_high": round(index_to_probability(indicator.index_high), 4),
                "breach": hydrograph.as_dict(),
            }
        )
    return out
