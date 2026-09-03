"""Pseudo-prospective monthly walk-forward, and the only place post-hoc labelling happens.

The separation of concerns here is the whole anti-hindsight argument, so it is worth being
explicit about it:

* `anomaly.py` scores units. It never learns which unit failed, when it failed, or that
  anything failed at all. It cannot: it imports nothing that could tell it.
* This module runs the walk-forward by calling `anomaly.walk_forward`, and **then**, once every
  score exists, asks which unit overlaps the AOI's `source_zone.geojson` so the report can say
  what that unit's tier was at each step. That is labelling, not modelling.

`tests/unit/watch/test_no_hindsight.py` enforces the split mechanically.

What is reported, per `PREREGISTRATION.md` section 7: the failed unit's tier at every monthly
step, the lead time from its first `watch` step to the failure, and — the number that actually
decides whether the tier is usable — how many *other* units were at `watch` on that same step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from serac.errors import SeracError
from serac.models.watch.aggregate import days_since_epoch, watch_cube_path
from serac.models.watch.anomaly import (
    ELEVATED_THRESHOLD,
    WATCH_THRESHOLD,
    InsufficientReason,
    Tier,
    UnitScore,
    UnitSeries,
    walk_forward,
)

BACKTEST_START = datetime(2016, 7, 1, tzinfo=UTC)


def monthly_steps(start: datetime, end: datetime) -> list[datetime]:
    """The first of each month in `[start, end]`, inclusive of a step exactly on `end`."""
    steps: list[datetime] = []
    year, month = start.year, start.month
    while True:
        step = datetime(year, month, 1, tzinfo=UTC)
        if step > end:
            return steps
        if step >= start:
            steps.append(step)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def load_series(data_dir: Path, aoi_id: str) -> dict[str, UnitSeries]:
    """Read `watch_cube.zarr` into the anomaly model's per-unit input structures."""
    import xarray as xr

    path = watch_cube_path(data_dir, aoi_id)
    if not path.exists():
        raise SeracError(f"no watch cube at {path}; run `serac watch aggregate` first")
    dataset = xr.open_zarr(path, consolidated=False)
    times = [
        datetime.fromisoformat(str(np.datetime_as_string(t, unit="s"))).replace(tzinfo=UTC)
        for t in dataset["time"].values
    ]
    t_days = np.array([days_since_epoch(t) for t in times], dtype=np.float64)
    units = [str(u) for u in dataset["unit"].values]
    los = np.asarray(dataset["los_displacement"].values, dtype=np.float64)
    coherence = np.asarray(dataset["temporal_coherence"].values, dtype=np.float64)
    sensitivity = np.asarray(dataset["los_sensitivity_signed"].values, dtype=np.float64)
    inside = np.asarray(dataset["inside_footprint"].values, dtype=bool)
    out: dict[str, UnitSeries] = {}
    for i, unit_id in enumerate(units):
        out[unit_id] = UnitSeries(
            unit_id=unit_id,
            t_days=t_days,
            los_mm=los[i, :],
            coherence=coherence[i, :],
            los_sensitivity_signed=float(sensitivity[i]) if np.isfinite(sensitivity[i]) else 0.0,
            inside_footprint=bool(inside[i]),
        )
    return out


# -- post-hoc labelling (reporting only) ---------------------------------------------------


def failed_unit_id(data_dir: Path, aoi_id: str) -> tuple[str | None, dict[str, Any]]:
    """The slope unit overlapping the AOI's source zone the most, per PREREGISTRATION section 7.

    **Post-hoc labelling.** This reads `source_zone.geojson`, which encodes where a failure is
    known to have happened, and it is therefore called only after every score exists. Nothing
    in the anomaly or threshold path may call it.
    """
    import geopandas as gpd
    from shapely.ops import unary_union

    from serac.models.watch.slope_units import slope_units_path

    zone_path = data_dir / "aoi" / aoi_id / "source_zone.geojson"
    if not zone_path.exists():
        return None, {"reason": f"no source zone at {zone_path}"}
    zones = gpd.read_file(zone_path)
    units = gpd.read_parquet(slope_units_path(data_dir, aoi_id))
    zone = unary_union(list(zones.to_crs(units.crs).geometry))
    hits = units.iloc[units.sindex.query(zone, predicate="intersects")]
    if len(hits) == 0:
        return None, {"reason": "no slope unit intersects the source zone"}
    overlaps = [
        (str(r.unit_id), float(r.geometry.intersection(zone).area)) for r in hits.itertuples()
    ]
    overlaps.sort(key=lambda p: (-p[1], p[0]))
    return overlaps[0][0], {
        "rule": "greatest area of overlap with data/aoi/<aoi>/source_zone.geojson",
        "n_units_intersecting_zone": len(overlaps),
        "overlap_m2": round(overlaps[0][1], 1),
        "runners_up": [{"unit_id": u, "overlap_m2": round(a, 1)} for u, a in overlaps[1:6]],
        "caveat": (
            "The source zone is a hand-digitised design rectangle in the AOI definition "
            "(geometry_quality hand_digitised_approximate, positional accuracy 1000 m), not a "
            "mapped detachment outline. The labelled unit is therefore approximate."
        ),
    }


@dataclass(frozen=True)
class StepSummary:
    """One monthly step, from the reporting side."""

    step: datetime
    target_tier: Tier
    target_score: float | None
    target_reason: str | None
    target_velocity: float | None
    n_watch: int
    n_elevated: int
    n_quiet: int
    n_insufficient: int
    n_other_watch: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.date().isoformat(),
            "target_tier": str(self.target_tier),
            "target_score": None
            if self.target_score is None or not np.isfinite(self.target_score)
            else round(self.target_score, 4),
            "target_reason": self.target_reason,
            "target_velocity_mm_yr": None
            if self.target_velocity is None
            else round(self.target_velocity, 3),
            "n_watch": self.n_watch,
            "n_elevated": self.n_elevated,
            "n_quiet": self.n_quiet,
            "n_insufficient_data": self.n_insufficient,
            "n_other_watch": self.n_other_watch,
        }


def summarise_steps(
    steps: list[datetime], scored: list[dict[str, UnitScore]], target: str | None
) -> list[StepSummary]:
    out: list[StepSummary] = []
    for step, results in zip(steps, scored, strict=True):
        tiers = [s.tier for s in results.values()]
        target_score = results.get(target) if target else None
        n_watch = sum(1 for t in tiers if t is Tier.watch)
        out.append(
            StepSummary(
                step=step,
                target_tier=target_score.tier if target_score else Tier.insufficient_data,
                target_score=target_score.score if target_score else None,
                target_reason=str(target_score.reason)
                if target_score and target_score.reason
                else None,
                target_velocity=target_score.velocity_mm_yr if target_score else None,
                n_watch=n_watch,
                n_elevated=sum(1 for t in tiers if t is Tier.elevated),
                n_quiet=sum(1 for t in tiers if t is Tier.quiet),
                n_insufficient=sum(1 for t in tiers if t is Tier.insufficient_data),
                n_other_watch=(
                    n_watch - 1 if target_score and target_score.tier is Tier.watch else n_watch
                ),
            )
        )
    return out


def run_backtest(
    *,
    data_dir: Path,
    reports_dir: Path,
    aoi_id: str,
    event_id: str,
    failure_time: datetime | None = None,
    start: datetime = BACKTEST_START,
) -> dict[str, Any]:
    """The walk-forward plus the write-up. Returns `{"summary": ..., "steps": ...}`."""
    series = load_series(data_dir, aoi_id)
    failure = failure_time or _event_time(data_dir, event_id)
    steps = monthly_steps(start, failure)
    if not steps:
        raise SeracError(f"no monthly steps between {start} and {failure}")
    scored = walk_forward(series, [days_since_epoch(s) for s in steps])
    target, labelling = failed_unit_id(data_dir, aoi_id)
    rows = summarise_steps(steps, scored, target)

    first_watch = next((r for r in rows if r.target_tier is Tier.watch), None)
    first_elevated = next((r for r in rows if r.target_tier in (Tier.elevated, Tier.watch)), None)
    summary: dict[str, Any] = {
        "aoi_id": aoi_id,
        "event_id": event_id,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "failure_time_utc": failure.isoformat(),
        "n_steps": len(rows),
        "first_step": rows[0].step.date().isoformat(),
        "last_step": rows[-1].step.date().isoformat(),
        "n_units_total": len(series),
        "labelled_unit": target,
        "labelling": labelling,
        "thresholds": {"elevated": ELEVATED_THRESHOLD, "watch": WATCH_THRESHOLD},
        "reached_watch": first_watch is not None,
        "lead_time_days_to_first_watch": (
            round((failure - first_watch.step).total_seconds() / 86_400.0, 1)
            if first_watch
            else None
        ),
        "concurrent_other_watch_units_at_first_watch": (
            first_watch.n_other_watch if first_watch else None
        ),
        "reached_elevated": first_elevated is not None,
        "lead_time_days_to_first_elevated": (
            round((failure - first_elevated.step).total_seconds() / 86_400.0, 1)
            if first_elevated
            else None
        ),
        "final_step_tier": str(rows[-1].target_tier),
        "final_step_reason": rows[-1].target_reason,
        "steps_by_target_tier": _tier_counts(rows),
        "median_watch_units_per_step": float(np.median([r.n_watch for r in rows])),
        "max_watch_units_per_step": max(r.n_watch for r in rows),
        "median_insufficient_units_per_step": float(np.median([r.n_insufficient for r in rows])),
        "disclaimer": (
            "The tier is ordinal. It is not a calibrated failure probability and it is never a "
            "prediction of a failure date. With one positive event no ROC is claimable."
        ),
    }
    payload: dict[str, Any] = {"summary": summary, "steps": [r.as_dict() for r in rows]}
    out = reports_dir / "watch" / f"backtest_{_slug(event_id)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    payload["report_path"] = out.as_posix()
    return payload


def _tier_counts(rows: list[StepSummary]) -> dict[str, int]:
    counts: dict[str, int] = {t.value: 0 for t in Tier}
    for row in rows:
        counts[row.target_tier.value] += 1
    return counts


def _slug(event_id: str) -> str:
    return event_id.split("-")[0]


def _event_time(data_dir: Path, event_id: str) -> datetime:
    """The event's origin time from the committed event record. Reporting-side only."""
    path = data_dir / "events" / f"{event_id}.json"
    if not path.exists():
        raise SeracError(f"no event record at {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    return datetime.fromisoformat(str(record["time"]["datetime_utc"]).replace("Z", "+00:00"))


def tier_table(
    *, data_dir: Path, aoi_id: str, as_of: datetime | None = None
) -> list[dict[str, Any]]:
    """Current tiers for an AOI, highest score first. Used by `serac watch tiers`."""
    series = load_series(data_dir, aoi_id)
    latest = max(float(s.t_days.max()) for s in series.values() if s.t_days.size)
    end = days_since_epoch(as_of) if as_of else latest
    steps = monthly_steps(BACKTEST_START, as_of or datetime.now(tz=UTC))
    scored = walk_forward(
        series, [days_since_epoch(s) for s in steps if days_since_epoch(s) <= end]
    )
    if not scored:
        return []
    final = scored[-1]
    rows: list[dict[str, Any]] = [
        {
            "unit_id": s.unit_id,
            "tier": str(s.tier),
            "score": float(s.score) if np.isfinite(s.score) else float("-inf"),
            "velocity": s.velocity_mm_yr,
            "n_samples": s.n_samples,
            "reason": str(s.reason) if s.reason else None,
        }
        for s in final.values()
    ]
    rows.sort(key=lambda r: (-float(r["score"]), str(r["unit_id"])))
    return rows


def observability_breakdown(
    scored: list[dict[str, UnitScore]],
) -> dict[str, Any]:
    """Split "we could not have seen it" from "there was no precursor", per section 8.

    The Langtang write-up turns on this distinction, so it is computed rather than asserted:
    a unit is *observable* at a step if it produced a score at all, and *quiet* only if it was
    observable and scored below the elevated threshold.
    """
    if not scored:
        return {"n_steps": 0}
    final = scored[-1]
    reasons: dict[str, int] = {r.value: 0 for r in InsufficientReason}
    for score in final.values():
        if score.reason is not None:
            reasons[score.reason.value] += 1
    observable = [s for s in final.values() if s.reason is None]
    ever_observable = {
        unit for step in scored for unit, score in step.items() if score.reason is None
    }
    return {
        "n_steps": len(scored),
        "n_units": len(final),
        "final_step_observable": len(observable),
        "final_step_insufficient_by_reason": reasons,
        "units_observable_at_any_step": len(ever_observable),
        "units_never_observable": len(final) - len(ever_observable),
        "final_step_quiet_and_observed": sum(1 for s in observable if s.tier is Tier.quiet),
        "final_step_elevated": sum(1 for s in observable if s.tier is Tier.elevated),
        "final_step_watch": sum(1 for s in observable if s.tier is Tier.watch),
    }
