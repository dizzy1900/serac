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
    TIER_ORDER,
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


def source_zone_elevation(data_dir: Path, aoi_id: str) -> dict[str, Any]:
    """Elevation statistics of the AOI's source zone, from the DEM under its polygon.

    **Post-hoc**, like everything else that reads `source_zone.geojson`, and reporting-only.
    It exists because the model card previously asserted an elevation span for the Ronti
    source zone that appeared in no artefact and was derivable from no code: it was in fact
    the labelled *unit's* band, not the zone's. A load-bearing physical figure has to be
    computed and committed or not used.
    """
    import rasterio.features
    from shapely.ops import unary_union

    from serac.models.watch.aggregate import ELEVATION_BANDS
    from serac.models.watch.raster import aoi_dem, grid_transform

    zone_path = data_dir / "aoi" / aoi_id / "source_zone.geojson"
    if not zone_path.exists():
        return {"available": False, "reason": f"no source zone at {zone_path}"}
    import geopandas as gpd

    dem = aoi_dem(data_dir, data_dir / "aoi" / aoi_id, aoi_id)
    zones = gpd.read_file(zone_path).to_crs(f"EPSG:{dem.grid.epsg}")
    geometry = unary_union(list(zones.geometry))
    mask = rasterio.features.rasterize(
        [(geometry, 1)],
        out_shape=(dem.grid.height, dem.grid.width),
        transform=grid_transform(dem.grid),
        fill=0,
        dtype="uint8",
    ).astype(bool)
    values = dem.elevation_m[mask & np.isfinite(dem.elevation_m)]
    if values.size == 0:
        return {"available": False, "reason": "the source zone covers no finite DEM pixel"}
    bands = []
    for low, high in ELEVATION_BANDS:
        inside = int(((values >= low) & (values < high)).sum())
        if inside:
            bands.append(
                {
                    "elevation_m": [low, high],
                    "n_pixels": inside,
                    "area_fraction": round(inside / values.size, 4),
                }
            )
    return {
        "available": True,
        "n_pixels": int(values.size),
        "area_km2": round(float(values.size) * dem.grid.resolution_m**2 / 1e6, 3),
        "min_m": round(float(values.min()), 1),
        "median_m": round(float(np.median(values)), 1),
        "max_m": round(float(values.max()), 1),
        "p05_m": round(float(np.percentile(values, 5)), 1),
        "p95_m": round(float(np.percentile(values, 95)), 1),
        "area_by_elevation_band": bands,
        "dem_source_sha256": dem.source_sha256,
    }


def source_zone_units(data_dir: Path, aoi_id: str) -> list[dict[str, Any]]:
    """Every slope unit intersecting the AOI source zone, with its overlap and geometry.

    **Post-hoc labelling**, like `failed_unit_id`, and used only for reporting. The
    pre-registered rule names a single unit — the largest overlap — but a source zone spans
    several aspects and a single track's sensitivity varies enormously between them, so
    reporting the whole neighbourhood is what makes an observability result interpretable
    instead of just negative. The headline number remains the pre-registered one.
    """
    import geopandas as gpd
    from shapely.ops import unary_union

    from serac.models.watch.slope_units import slope_units_path

    zone_path = data_dir / "aoi" / aoi_id / "source_zone.geojson"
    if not zone_path.exists():
        return []
    zones = gpd.read_file(zone_path)
    units = gpd.read_parquet(slope_units_path(data_dir, aoi_id))
    zone = unary_union(list(zones.to_crs(units.crs).geometry))
    hits = units.iloc[units.sindex.query(zone, predicate="intersects")]
    rows: list[dict[str, Any]] = []
    for r in hits.itertuples():
        rows.append(
            {
                "unit_id": str(r.unit_id),
                "overlap_m2": round(float(r.geometry.intersection(zone).area), 1),
                "aspect_deg": round(float(r.aspect_deg), 1),
                "mean_slope_deg": round(float(r.mean_slope_deg), 1),
                "area_m2": round(float(r.area_m2), 1),
                "glacier_cover": None if r.glacier_cover is None else bool(r.glacier_cover),
            }
        )
    rows.sort(key=lambda x: (-float(x["overlap_m2"] or 0.0), str(x["unit_id"])))
    return rows


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
    neighbourhood = _source_zone_history(data_dir, aoi_id, series, scored, steps, failure)

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
        "source_zone_neighbourhood": neighbourhood,
        "observability": observability_breakdown(scored),
        "source_zone_summary": _neighbourhood_counts(neighbourhood),
        "source_zone_elevation": source_zone_elevation(data_dir, aoi_id),
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
    slug = _slug(event_id)
    out = reports_dir / "watch" / f"backtest_{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    from serac.models.watch.writeup import (
        backtest_markdown,
        gather_context,
        langtang_markdown,
        write_markdown,
    )

    context = gather_context(data_dir, reports_dir, aoi_id)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    if slug == "langtang":
        text = langtang_markdown(payload, summary["observability"], context)
    else:
        text = backtest_markdown(payload, context)
    markdown = write_markdown(text, reports_dir / "watch" / f"backtest_{slug}.md")
    payload["report_path"] = out.as_posix()
    payload["markdown_path"] = markdown.as_posix()
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


def _source_zone_history(
    data_dir: Path,
    aoi_id: str,
    series: dict[str, UnitSeries],
    scored: list[dict[str, UnitScore]],
    steps: list[datetime],
    failure: datetime,
) -> list[dict[str, Any]]:
    """Tier history of every source-zone unit, so an observability result can be read properly.

    Reporting only. It answers the question a reader will immediately ask when the labelled
    unit turns out to be unobservable: was any part of the source zone observable, and if so
    what did it show?
    """
    out: list[dict[str, Any]] = []
    for row in source_zone_units(data_dir, aoi_id):
        unit_id = str(row["unit_id"])
        unit_series = series.get(unit_id)
        history = [step.get(unit_id) for step in scored]
        measurable = [s for s in history if s is not None and s.reason is None]
        first_watch = next(
            (steps[i] for i, s in enumerate(history) if s is not None and s.tier is Tier.watch),
            None,
        )
        best = max(
            (TIER_ORDER[s.tier] for s in history if s is not None),
            default=TIER_ORDER[Tier.insufficient_data],
        )
        final = history[-1] if history else None
        out.append(
            {
                **row,
                "los_sensitivity_signed": (
                    round(unit_series.los_sensitivity_signed, 4) if unit_series else None
                ),
                "steps_measurable": len(measurable),
                "steps_total": len(history),
                # `ever_measurable` is the honest quantifier. An earlier version reported the
                # last non-None reason found anywhere in the history and then treated a unit
                # as measurable only when that was absent, which silently meant "measurable at
                # EVERY step" while the prose said "at any step". A unit measurable at 38 of
                # 122 steps was counted as never measurable.
                "ever_measurable": bool(measurable),
                "best_tier_reached": next(t.value for t, v in TIER_ORDER.items() if v == best),
                "first_watch_step": first_watch.date().isoformat() if first_watch else None,
                "lead_time_days_to_first_watch": (
                    round((failure - first_watch).total_seconds() / 86_400.0, 1)
                    if first_watch
                    else None
                ),
                "final_step_tier": final.tier.value if final else None,
                "final_step_reason": (
                    str(final.reason) if final is not None and final.reason is not None else None
                ),
            }
        )
    return out


def _neighbourhood_counts(neighbourhood: list[dict[str, Any]]) -> dict[str, Any]:
    """Split the source zone by whether a unit was **ever** measurable, and by what it showed.

    Two separate questions, kept separate: how many units the sensor could see at all, and what
    the ones it could see actually did. Collapsing them is what produced a false "0 of 48
    measurable" headline while the same file's table showed a unit measurable at 38 steps.
    """
    ever = [r for r in neighbourhood if r["ever_measurable"]]
    never = [r for r in neighbourhood if not r["ever_measurable"]]
    by_reason: dict[str, int] = {}
    for row in never:
        key = str(row.get("final_step_reason") or "unknown")
        by_reason[key] = by_reason.get(key, 0) + 1
    by_tier: dict[str, int] = {}
    for row in ever:
        key = str(row["best_tier_reached"])
        by_tier[key] = by_tier.get(key, 0) + 1
    return {
        "units_total": len(neighbourhood),
        "units_ever_measurable": len(ever),
        "units_never_measurable": len(never),
        "never_measurable_by_final_step_reason": by_reason,
        "ever_measurable_by_best_tier": by_tier,
        "ever_measurable_units": [
            {
                "unit_id": r["unit_id"],
                "steps_measurable": r["steps_measurable"],
                "steps_total": r["steps_total"],
                "best_tier_reached": r["best_tier_reached"],
                "los_sensitivity_signed": r["los_sensitivity_signed"],
                "aspect_deg": r["aspect_deg"],
                "first_watch_step": r["first_watch_step"],
            }
            for r in sorted(
                ever, key=lambda x: (-TIER_ORDER_BY_NAME[x["best_tier_reached"]], x["unit_id"])
            )
        ],
    }


TIER_ORDER_BY_NAME: dict[str, int] = {t.value: v for t, v in TIER_ORDER.items()}
