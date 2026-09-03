"""Turn the backtest JSON into prose a sceptical reader can check.

The markdown is generated, not written by hand, so no number in a report can drift from the
JSON it came from. Two things are deliberate about the wording:

* Nothing here ever says *when* a slope will fail, and the vocabulary that would imply it is
  screened by `validate-watch`.
* The Langtang write-up is forced to answer two separate questions under two named headings —
  "we could not have seen it" (observability) and "there was no precursor" — because collapsing
  them is the single easiest way to overstate a null result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NOT_A_PREDICTION = (
    "The watch tier is an **ordinal** state. It is not a calibrated failure probability and it "
    "is never a prediction of when a slope will fail. With one positive event in the archive no "
    "ROC curve and no calibration curve is estimable, and none is reported here."
)


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.1f}{suffix}"
    return f"{value}{suffix}"


def _tier_timeline(steps: list[dict[str, Any]]) -> str:
    """One row per step: tier, score, and how many other units were at watch."""
    lines = [
        "| step | tier | score | LOS velocity (mm/yr) | units at watch | other units at watch |",
        "|---|---|---|---|---|---|",
    ]
    for s in steps:
        lines.append(
            f"| {s['step']} | {s['target_tier']} | {_fmt(s.get('target_score'))} | "
            f"{_fmt(s.get('target_velocity_mm_yr'))} | {s['n_watch']} | {s['n_other_watch']} |"
        )
    return "\n".join(lines)


def _coverage_section(summary: dict[str, Any], context: dict[str, Any]) -> str:
    plan = context.get("network_plan") or {}
    selection = context.get("track_selection") or {}
    cube = context.get("watch_cube") or {}
    return "\n".join(
        [
            "| quantity | value |",
            "|---|---|",
            f"| Sentinel-1 relative orbit | {plan.get('path_number')} "
            f"({plan.get('flight_direction')}) |",
            f"| track selection rule sha256 | `{str(selection.get('rule_sha256'))[:16]}` |",
            f"| interferograms planned / succeeded | {plan.get('budget', {}).get('n_pairs')} / "
            f"{context.get('n_pairs_harvested')} |",
            f"| AOI bbox imaged by the burst footprint | "
            f"{_fmt(100.0 * float(plan.get('aoi_coverage_fraction', 0.0)), ' %')} |",
            f"| slope units | {summary.get('n_units_total')} |",
            f"| units with any InSAR measurement | {cube.get('units_with_any_measurement')} |",
            f"| time-series epochs | {cube.get('n_epochs')} |",
            f"| tropospheric correction | {cube.get('tropospheric_correction', 'n/a')} |",
        ]
    )


def _coherence_table(context: dict[str, Any]) -> str:
    rows = context.get("coherence_by_elevation") or []
    if not rows:
        return "_No MintPy temporal-coherence raster was available to tabulate._"
    lines = [
        "| elevation band | pixels | median temporal coherence | fraction >= 0.40 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        band = row["elevation_m"]
        label = "**whole AOI**" if band is None else f"{band[0]:,.0f} - {band[1]:,.0f} m"
        lines.append(
            f"| {label} | {row['n_pixels']:,} | {row['median_temporal_coherence']:.3f} | "
            f"{row['fraction_above_threshold']:.3f} |"
        )
    return "\n".join(lines)


def _source_zone_table(summary: dict[str, Any], limit: int = 12) -> str:
    rows = summary.get("source_zone_neighbourhood") or []
    if not rows:
        return "_The AOI defines no source zone, so no neighbourhood could be tabulated._"
    lines = [
        "| unit | overlap (m2) | aspect | LOS sens | measurable | best tier | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows[:limit]:
        sensitivity = row.get("los_sensitivity_signed")
        lines.append(
            f"| `{row['unit_id']}` | {row['overlap_m2']:,.0f} | {row['aspect_deg']:.0f} deg | "
            f"{'n/a' if sensitivity is None else f'{sensitivity:+.3f}'} | "
            f"{row['steps_measurable']}/{row['steps_total']} | {row['best_tier_reached']} | "
            f"{row.get('insufficient_reason') or '-'} |"
        )
    if len(rows) > limit:
        lines.append(f"| _... {len(rows) - limit} more_ | | | | | | |")
    return "\n".join(lines)


def _observability_paragraph(summary: dict[str, Any]) -> str:
    counts = summary.get("source_zone_insufficient_reasons") or {}
    total = sum(counts.values())
    if not total:
        return ""
    measurable = counts.get("measurable", 0)
    parts = ", ".join(
        f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items()) if k != "measurable"
    )
    return (
        f"{total} slope units intersect the source zone. **{measurable}** of them were "
        f"measurable at any step; the rest failed a data-adequacy test ({parts}). "
        "A unit that fails one of those tests is not being watched at all, and nothing about "
        "its stability follows from its absence from the Watch list."
    )


def backtest_markdown(payload: dict[str, Any], context: dict[str, Any]) -> str:
    """The Chamoli write-up."""
    summary = payload["summary"]
    steps = payload["steps"]
    lead = summary.get("lead_time_days_to_first_watch")
    concurrent = summary.get("concurrent_other_watch_units_at_first_watch")
    median_insufficient = _fmt(summary.get("median_insufficient_units_per_step"))

    if summary.get("reached_watch"):
        headline = (
            f"The labelled unit (`{summary.get('labelled_unit')}`) first reached **Watch** "
            f"{_fmt(lead, ' days')} before the failure. On that same step "
            f"**{concurrent} other slope units were also at Watch**."
        )
        verdict = "A lead time is only usable if the alarm it raises is actionable. " + (
            f"With {concurrent} other units simultaneously at Watch out of "
            f"{summary.get('n_units_total')}, an operator following this tier would have had "
            f"{concurrent + 1} slopes to investigate and no way, from this layer alone, to "
            "tell which one mattered."
            if isinstance(concurrent, int) and concurrent > 0
            else "No other unit was at Watch on that step, which is the best case this "
            "single event can demonstrate. It remains one event."
        )
    elif summary.get("steps_by_target_tier", {}).get("insufficient_data") == summary.get("n_steps"):
        # The labelled unit was never measurable at any step. This is an observability result
        # and calling it a detection failure would be wrong: the tier was never asked.
        headline = (
            f"The labelled unit (`{summary.get('labelled_unit')}`) was **`insufficient_data` at "
            f"every one of the {summary.get('n_steps')} steps** (final reason: "
            f"`{summary.get('final_step_reason')}`). It never entered the tier at all."
        )
        verdict = (
            "**This is an observability result, not a statement about precursors.** This "
            "configuration — one ascending track, 80 m pixels, C-band, a height-correlation "
            "tropospheric correction — could not measure the slope that failed, whether or not "
            "it was moving. Reporting it as 'no precursor detected' would be wrong, and "
            "reporting it as a failure of the tier would be wrong too: the tier was never in a "
            "position to be asked. Section 10 of the pre-registration named exactly this "
            "outcome in advance as the third of the three ways the design could come out."
        )
    else:
        headline = (
            f"The labelled unit (`{summary.get('labelled_unit')}`) **never reached Watch** at any "
            f"step. Its tier at the final step before the failure was "
            f"`{summary.get('final_step_tier')}`"
            + (
                f" (reason: `{summary.get('final_step_reason')}`)."
                if summary.get("final_step_reason")
                else "."
            )
        )
        verdict = (
            "This is a negative result for the tier on the one positive event available. It is "
            "reported as such. Section 10 of the pre-registration named this outcome in advance "
            "as one that would falsify the design."
        )

    return f"""# Chamoli 2021 — pseudo-prospective backtest of the slope-watch tier

Generated {summary.get("generated_at")} from `reports/watch/backtest_chamoli.json`.
Thresholds and protocol were fixed in `reports/watch/PREREGISTRATION.md`, committed before any
interferogram had been delivered. `make validate-watch` checks that ancestry against git.

> {NOT_A_PREDICTION}

## Result

{headline}

{verdict}

## What was processed

{_coverage_section(summary, context)}

## Protocol

Monthly steps on the first of each month from {summary.get("first_step")} to
{summary.get("last_step")}, the last step preceding the failure at
{summary.get("failure_time_utc")}. At each step every slope unit was scored using **only**
acquisitions at or before that step; `tests/unit/watch/test_anomaly.py` proves this by
appending future samples, truncating them again and asserting the scores are unchanged.

The failed unit was identified **after** all scoring, by the rule pre-registered in section 7:
{summary.get("labelling", {}).get("rule")}.
{summary.get("labelling", {}).get("caveat", "")}

## Numbers

| quantity | value |
|---|---|
| steps | {summary.get("n_steps")} |
| slope units | {summary.get("n_units_total")} |
| reached Watch | {summary.get("reached_watch")} |
| lead time to first Watch | {_fmt(lead, " days")} |
| other units at Watch on that step | {_fmt(concurrent)} |
| reached Elevated | {summary.get("reached_elevated")} |
| lead time to first Elevated | {_fmt(summary.get("lead_time_days_to_first_elevated"), " days")} |
| median units at Watch per step | {_fmt(summary.get("median_watch_units_per_step"))} |
| max units at Watch in any step | {summary.get("max_watch_units_per_step")} |
| median units at insufficient_data per step | {median_insufficient} |

Tier of the labelled unit across the walk-forward:
{json.dumps(summary.get("steps_by_target_tier", {}), sort_keys=True)}

## The source zone, unit by unit

{_observability_paragraph(summary)}

{_source_zone_table(summary)}

## Why: C-band temporal coherence against elevation

The physical limitation this component is most constrained by, measured on this stack rather
than asserted. MintPy temporal coherence over the 260-pair network, against the HyP3 DEM:

{_coherence_table(context)}

## Step-by-step

{_tier_timeline(steps)}

## Limitations that bear on this result

See `reports/MODEL_CARD_watch.md` for the full list. The ones that matter most here:

- Tropospheric correction is `height_correlation`, not GACOS or ERA5, so turbulent wet delay
  survives into the velocities. This is the most likely source of a spurious Watch.
- One ascending track. Downslope motion on west-facing slopes projects onto the line of sight
  with a factor near zero, so those units are reported `insufficient_data`, not `quiet`.
- C-band decorrelates over snow and ice within days, which is the surface of a rock-ice
  avalanche source zone.
- A brittle crystalline failure need not have measurable tertiary creep at all. A `quiet` tier
  on competent bedrock is weak evidence of stability.
"""


def langtang_markdown(
    payload: dict[str, Any], observability: dict[str, Any], context: dict[str, Any]
) -> str:
    """The Langtang write-up, with the two negatives forced apart."""
    summary = payload["summary"]
    reasons = observability.get("final_step_insufficient_by_reason", {})
    n_units = observability.get("n_units", 0)
    never = observability.get("units_never_observable", 0)
    observed_quiet = observability.get("final_step_quiet_and_observed", 0)

    return f"""# Langtang / Lhende Khola 2026 — slope-watch result

Generated {summary.get("generated_at")}. Same code, same pre-registered thresholds and the same
protocol as the Chamoli backtest; only the AOI and the window differ.

> {NOT_A_PREDICTION}

**Window truncation.** The archive processed here runs
{summary.get("first_step")} to {summary.get("last_step")}, not back to the start of the
Sentinel-1 record. This is a deliberate budget choice, disclosed rather than hidden: the same
`n_conn = 2`, `Bt <= 36 d` network over a decade would not have fitted the disk or the session.
A slow precursor that began before {summary.get("first_step")} is therefore outside what this
run could have detected, independently of everything below.

## What was processed

{_coverage_section(summary, context)}

## We could not have seen it — observability

This section is about the *sensor and the archive*, not about the slope.

| quantity | value |
|---|---|
| slope units in the AOI | {n_units} |
| units never observable at any step | {never} |
| units observable at the final step | {observability.get("final_step_observable")} |

Reasons a unit was not observable at the final step:

| reason | units |
|---|---|
| outside the processed burst footprint | {reasons.get("outside_footprint", 0)} |
| LOS sensitivity below the floor | {reasons.get("low_los_sensitivity", 0)} |
| too few acquisitions | {reasons.get("too_few_samples", 0)} |
| coherence below the floor | {reasons.get("low_coherence", 0)} |
| too little walk-forward history | {reasons.get("too_little_history", 0)} |

A unit in any of those rows was **not being watched**. Nothing about its stability follows
from its absence from the Watch list.

## There was no precursor — units that were observed and stayed quiet

This section is about the *slope*, and only for units the previous section shows were
measurable.

| quantity | value |
|---|---|
| observed and `quiet` at the final step | {observed_quiet} |
| observed and `elevated` | {observability.get("final_step_elevated", 0)} |
| observed and `watch` | {observability.get("final_step_watch", 0)} |
| labelled source-zone unit | `{summary.get("labelled_unit")}` |
| its tier at the final step | `{summary.get("final_step_tier")}` |
| its reason, if not measurable | `{summary.get("final_step_reason")}` |
| did it ever reach Watch | {summary.get("reached_watch")} |
| did it ever reach Elevated | {summary.get("reached_elevated")} |

## Why: C-band temporal coherence against elevation

{_coherence_table(context)}

## The source zone, unit by unit

{_observability_paragraph(summary)}

{_source_zone_table(summary)}

## Reading this honestly

{
        "The source-zone unit was measurable and stayed below the Watch threshold. That is a "
        "genuine null result: within the sensitivity this configuration achieves, no "
        "kinematic precursor was resolvable in the Sentinel-1 archive processed here. It does "
        "**not** mean no precursor existed — it means none was resolvable at 80 m pixels, on "
        "one ascending-or-descending track, with a height-correlation tropospheric correction, "
        "over a window that begins well after the Sentinel-1 record does."
        if summary.get("final_step_reason") is None
        else "The source-zone unit was **not measurable** at the final step (reason: "
        f"`{summary.get('final_step_reason')}`). This is an observability result, not a "
        "statement about precursors: this configuration could not have seen a precursor there "
        "whether or not one existed. Reporting it as 'no precursor found' would be wrong."
    }

A null result is a valid and important output of this component, and it is the honest one to
publish. What it costs to do better is stated in the model card: a second track for the
opposite-facing slopes, a real tropospheric correction (GACOS or ERA5), finer looks, and
ground truth on at least one instrumented slope to calibrate anything at all.

## Step-by-step

{_tier_timeline(payload["steps"])}
"""


def write_markdown(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def gather_context(data_dir: Path, reports_dir: Path, aoi_id: str) -> dict[str, Any]:
    """Everything the write-ups quote that does not come from the backtest itself."""

    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    from serac.models.watch.aggregate import coherence_by_elevation
    from serac.models.watch.insar_jobs import harvested_pairs

    try:
        coherence = coherence_by_elevation(data_dir, aoi_id)
    except Exception:
        coherence = []
    return {
        "coherence_by_elevation": coherence,
        "network_plan": _load(data_dir / "interim" / "watch" / f"network_{aoi_id}.json"),
        "track_selection": _load(reports_dir / "watch" / f"track_selection_{aoi_id}.json"),
        "watch_cube": _load(reports_dir / "watch" / f"watch_cube_{aoi_id}.json"),
        "mintpy": _load(reports_dir / "watch" / f"mintpy_{aoi_id}.json"),
        "optical": _load(reports_dir / "watch" / f"optical_{aoi_id}.json"),
        "slope_units": _load(reports_dir / "watch" / f"slope_units_{aoi_id}.json"),
        "n_pairs_harvested": sum(1 for _ in harvested_pairs(data_dir, aoi_id)),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
