"""v0 robust-z anomaly model behind `WatchAnomalyModel`.

Wraps the pre-registered functions in `anomaly.py`. It does not change thresholds, windows, or
the scoring rule. Spatial z uses every unit present in the cube view; history is the walk-forward
over sample times at or before `as_of`.

Required layers are the watch-cube variables the v0 model actually reads
(`los_displacement`, `temporal_coherence`), not the EO feature-cube names. Sensitivity and
footprint come from the view's static fields. The EO feature cube is v1's input.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from serac.domain.watch import WatchTier
from serac.domain.watch_model import UnitWatchScore, WatchScoreInsufficientReason
from serac.models.watch import WATCH_VERSION
from serac.models.watch.aggregate import days_since_epoch
from serac.models.watch.anomaly import UnitSeries, walk_forward
from serac.ports.watch import (
    FeatureCubeView,
    LayerSeries,
    WatchAnomalyModel,
    WatchModelInfo,
    causal_max_input_time,
)

ROBUST_Z_NAME = "robust-z-v0"
V0_REQUIRED_LAYERS: tuple[str, ...] = ("los_displacement", "temporal_coherence")
LOS_SENSITIVITY_FIELD = "los_sensitivity_signed"
INSIDE_FOOTPRINT_FIELD = "inside_footprint"


class RobustZWatch(WatchAnomalyModel):
    """Pre-registered v0 scorer, adapted to the watch port without changing its numbers."""

    @property
    def required_layers(self) -> tuple[str, ...]:
        return V0_REQUIRED_LAYERS

    def info(self) -> WatchModelInfo:
        return WatchModelInfo(
            name=ROBUST_Z_NAME,
            version=WATCH_VERSION,
            is_fitted=True,
            required_layers=self.required_layers,
            notes=(
                "Closed-form robust z-score; nothing to fit. Score is not a probability "
                "and never a predicted date of collapse."
            ),
        )

    def score_unit(
        self, unit_id: str, as_of: datetime, cube_view: FeatureCubeView
    ) -> UnitWatchScore:
        """Score `unit_id` with the pre-registered walk-forward, using `t <= as_of` only."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        max_input_time = causal_max_input_time(cube_view, unit_id, self.required_layers, as_of)
        if unit_id not in cube_view.units():
            return self._unassessed(
                unit_id, as_of, WatchScoreInsufficientReason.unit_not_in_view, max_input_time
            )
        for layer in self.required_layers:
            if not cube_view.has_layer(unit_id, layer):
                return self._unassessed(
                    unit_id, as_of, WatchScoreInsufficientReason.missing_layers, max_input_time
                )

        series_by_unit: dict[str, UnitSeries] = {}
        for uid in cube_view.units():
            if not all(cube_view.has_layer(uid, layer) for layer in self.required_layers):
                continue
            series_by_unit[uid] = _unit_series_from_view(cube_view, uid)
        if unit_id not in series_by_unit:
            return self._unassessed(
                unit_id, as_of, WatchScoreInsufficientReason.missing_layers, max_input_time
            )

        end = days_since_epoch(as_of)
        sample_days = sorted(
            {float(t) for series in series_by_unit.values() for t in series.t_days if t <= end}
        )
        steps = sample_days if sample_days and sample_days[-1] == end else [*sample_days, end]
        scored = walk_forward(series_by_unit, steps)
        raw = scored[-1][unit_id]
        reason = None if raw.reason is None else WatchScoreInsufficientReason(raw.reason.value)
        score = None if reason is not None or not np.isfinite(raw.score) else float(raw.score)
        return UnitWatchScore(
            unit_id=unit_id,
            as_of=as_of,
            score=score,
            tier=WatchTier(raw.tier.value),
            insufficient_reason=reason,
            model_name=ROBUST_Z_NAME,
            model_version=WATCH_VERSION,
            max_input_time=max_input_time,
        )

    def _unassessed(
        self,
        unit_id: str,
        as_of: datetime,
        reason: WatchScoreInsufficientReason,
        max_input_time: datetime | None,
    ) -> UnitWatchScore:
        return UnitWatchScore(
            unit_id=unit_id,
            as_of=as_of,
            score=None,
            tier=WatchTier.insufficient_data,
            insufficient_reason=reason,
            model_name=ROBUST_Z_NAME,
            model_version=WATCH_VERSION,
            max_input_time=max_input_time,
        )


def _unit_series_from_view(cube_view: FeatureCubeView, unit_id: str) -> UnitSeries:
    displacement = cube_view.series(unit_id, "los_displacement")
    coherence = cube_view.series(unit_id, "temporal_coherence")
    times, los_mm, coh = _align(displacement, coherence)
    sensitivity = cube_view.static_value(unit_id, LOS_SENSITIVITY_FIELD)
    inside = cube_view.flag(unit_id, INSIDE_FOOTPRINT_FIELD)
    return UnitSeries(
        unit_id=unit_id,
        t_days=np.array([days_since_epoch(t) for t in times], dtype=np.float64),
        los_mm=np.array(los_mm, dtype=np.float64),
        coherence=np.array(coh, dtype=np.float64),
        los_sensitivity_signed=0.0 if sensitivity is None else float(sensitivity),
        inside_footprint=True if inside is None else bool(inside),
    )


def _align(
    displacement: LayerSeries, coherence: LayerSeries
) -> tuple[tuple[datetime, ...], tuple[float, ...], tuple[float, ...]]:
    """Inner-join the two layers on timestamp. v0 assumes a shared time axis."""
    coh_by_time = dict(zip(coherence.times, coherence.values, strict=True))
    times: list[datetime] = []
    los: list[float] = []
    coh: list[float] = []
    for time, value in zip(displacement.times, displacement.values, strict=True):
        if time in coh_by_time:
            times.append(time)
            los.append(value)
            coh.append(coh_by_time[time])
    return tuple(times), tuple(los), tuple(coh)
