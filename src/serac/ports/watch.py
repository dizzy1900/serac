"""Port for anything that turns a feature-cube window into a per-unit watch score.

v0 is a pre-registered robust-z procedure over MintPy unit series. v1 is a self-supervised
autoencoder over the EO feature cube. They share this port so a caller can swap implementations
without changing types. `fit` is intentionally absent: v0 has nothing to fit, and v1 has no
trained weights in this repository.

**Causality.** `score_unit` may only consume samples with `t <= as_of`. Inclusive trailing
windows (the v0 rule in `anomaly.py`) allow a sample at exactly `as_of`. A view may contain
later samples; the model must drop them. `UnitWatchScore.max_input_time` reports the latest
sample actually used and must be `<= as_of`.

**The score is not a failure date and not a probability of collapse.** It is an anomaly
statistic. An unfitted or unmeasurable unit returns `insufficient_data` (or a null tier) with a
reason. It never returns Quiet as a stand-in for "we could not score this."
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from serac.domain.watch_model import UnitWatchScore

WATCH_PORT_VERSION = "0.1.0"


class WatchModelInfo(BaseModel):
    """What a watch model says about itself. Recorded next to every score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    is_fitted: bool = Field(
        description=(
            "False when score_unit cannot assess a unit because weights are absent. "
            "v0 is a closed-form statistic and reports True."
        )
    )
    required_layers: tuple[str, ...] = Field(
        description="Cube layer names the model reads. Samples with t > as_of are ignored."
    )
    weights_path: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class LayerSeries:
    """One named cube layer for one unit. Times are timezone-aware UTC and strictly sorted.

    The series MAY contain samples after `as_of`. The model MUST ignore `t > as_of`.
    """

    times: tuple[datetime, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.times) != len(self.values):
            raise ValueError("times and values must be the same length")
        if any(t.tzinfo is None for t in self.times):
            raise ValueError("layer times must be timezone-aware")
        if len(set(self.times)) != len(self.times):
            raise ValueError("layer times must be unique")
        if any(earlier > later for earlier, later in zip(self.times, self.times[1:], strict=False)):
            raise ValueError("layer times must be sorted")

    def upto(self, as_of: datetime) -> LayerSeries:
        """Samples with `t <= as_of` only — the inclusive causal window."""
        times: list[datetime] = []
        values: list[float] = []
        for time, value in zip(self.times, self.values, strict=True):
            if time <= as_of:
                times.append(time)
                values.append(value)
        return LayerSeries(times=tuple(times), values=tuple(values))


class FeatureCubeView:
    """Read-only window over cube layers for one or more slope units.

    This is an in-memory view, not a Zarr handle. Tests construct it from arrays. A production
    adapter can later slice the feature cube or the watch cube into the same shape.

    Implementations MAY contain samples with `t > as_of`. Every `WatchAnomalyModel` MUST ignore
    those samples. Inclusive trailing windows match `anomaly.py`: `t <= as_of`.
    """

    def units(self) -> Sequence[str]:
        raise NotImplementedError

    def has_layer(self, unit_id: str, layer: str) -> bool:
        raise NotImplementedError

    def series(self, unit_id: str, layer: str) -> LayerSeries:
        raise NotImplementedError

    def static_value(self, unit_id: str, name: str) -> float | None:
        raise NotImplementedError

    def flag(self, unit_id: str, name: str) -> bool | None:
        raise NotImplementedError


@dataclass(frozen=True)
class InMemoryCubeView(FeatureCubeView):
    """A `FeatureCubeView` built from arrays. Times may extend past `as_of`."""

    layers: Mapping[str, Mapping[str, LayerSeries]]
    static: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    flags: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)

    def units(self) -> tuple[str, ...]:
        return tuple(self.layers)

    def has_layer(self, unit_id: str, layer: str) -> bool:
        return layer in self.layers.get(unit_id, {})

    def series(self, unit_id: str, layer: str) -> LayerSeries:
        return self.layers[unit_id][layer]

    def static_value(self, unit_id: str, name: str) -> float | None:
        values = self.static.get(unit_id)
        if values is None:
            return None
        return values.get(name)

    def flag(self, unit_id: str, name: str) -> bool | None:
        flags = self.flags.get(unit_id)
        if flags is None:
            return None
        return flags.get(name)


def causal_max_input_time(
    cube_view: FeatureCubeView,
    unit_id: str,
    layers: Sequence[str],
    as_of: datetime,
) -> datetime | None:
    """Latest sample among `layers` for `unit_id` with `t <= as_of`, or None if none exist.

    This is the port's causality rule as a function: a model that reports a later
    `max_input_time` has peeked.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    latest: datetime | None = None
    for layer in layers:
        if not cube_view.has_layer(unit_id, layer):
            continue
        for time in cube_view.series(unit_id, layer).times:
            if time <= as_of and (latest is None or time > latest):
                latest = time
    return latest


class WatchAnomalyModel(ABC):
    """Scores one slope unit from a cube view, causally, as of a given time.

    `fit` is not part of this port. v0 has nothing to fit. v1 has no trained weights here.
    """

    @abstractmethod
    def info(self) -> WatchModelInfo:
        """Identify the model, its layers, and whether it can actually score."""

    @property
    @abstractmethod
    def required_layers(self) -> tuple[str, ...]:
        """Cube layer names the model reads. Samples with `t > as_of` are ignored."""

    @abstractmethod
    def score_unit(
        self, unit_id: str, as_of: datetime, cube_view: FeatureCubeView
    ) -> UnitWatchScore:
        """Score `unit_id` using only samples with `t <= as_of`.

        The returned score is not a failure date and not a probability of collapse. When the
        model cannot assess the unit (missing weights, missing layers, v0 measurability), the
        result is `insufficient_data` / a reason — never Quiet, never a fabricated 0.0 residual
        that looks like stability.
        """
