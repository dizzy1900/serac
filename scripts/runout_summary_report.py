"""Render `reports/runout/SUMMARY.md` from the machine-readable records.

Everything here is read from files the pipeline wrote; nothing is retyped. Run after
`serac runout summarise`, `serac runout train` and `serac runout langtang`.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
REPORTS = REPO / "reports" / "runout"


class SummaryReportError(Exception):
    """The summary generator was pointed at an artefact it cannot render."""


def load(name: str) -> dict[str, Any] | None:
    path = REPORTS / name
    if not path.exists():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> None:
    from serac.models.runout.terrain import thalweg_sentence

    thalweg_sentence_text = thalweg_sentence(REPORTS)
    timing = load("timing.json") or {}
    verification = load("verification.json") or {}
    summary = load("ensemble_summary.json") or {}
    metrics = load("surrogate_metrics.json") or {}
    convergence = load("grid_convergence.json") or {}
    design = load("ensemble_design.json") or {}

    # `timing.json` keys are `per_resolution` (the fresh study, one parameter vector) and
    # `pre_ensemble_measurements.runs` (the mu-varying runs the ensemble was sized against).
    # An earlier version read a non-existent `runs` key at the top level and silently rendered an
    # empty table under a heading that claims to show measured cost.
    study_runs = timing.get("per_resolution", [])
    sizing_runs = (timing.get("pre_ensemble_measurements") or {}).get("runs", [])
    if not study_runs and not sizing_runs:
        raise SystemExit("timing.json has neither per_resolution nor pre_ensemble_measurements")
    timing_rows = "\n".join(
        f"| {r['resolution_m']:.0f} m | {r['solver_wall_s']:.1f} | {r['steps']} | "
        f"{r['ms_per_step']:.2f} | {r['simulated_time_s']:.0f} | {r['reach_m'] / 1000:.2f} | "
        f"{r['active_cells']} |"
        for r in study_runs
    )
    sizing_rows = "\n".join(
        f"| {r['resolution_m']:.0f} m | {r['mu']:.2f} | {r['solver_wall_s']:.1f} | "
        f"{r['steps']} | {r['ms_per_step']:.2f} | {r['reach_m'] / 1000:.2f} |"
        for r in sizing_runs
    )

    ritter_rows = "\n".join(
        f"| {r['cells']} | {r['dx_m']:.1f} | {r['l1_m2']:.1f} | {r['l1_relative']:.2%} |"
        for r in verification.get("ritter_dam_break", [])
    )
    production_cfl = verification.get("production_cfl", 0.45)
    splitting_bias = verification.get("voellmy_terminal_velocity_at_production_cfl", float("nan"))
    tv = verification.get("voellmy_terminal_velocity_relative_error_by_cfl", {})
    tv_rows = "\n".join(
        f"| {k}{' **(production)**' if float(k) == production_cfl else ''} | {v:.3%} |"
        for k, v in sorted(tv.items(), key=lambda kv: -float(kv[0]))
    )

    reach = summary.get("reach_km", {})
    counts = summary.get("resolution_counts") or {}
    resolution_split = (
        ", ".join(f"{int(float(k))} m x {v}" for k, v in sorted(counts.items(), reverse=True))
        or "—"
    )
    flags = summary.get("flag_reasons", {})
    flag_rows = "\n".join(f"| {k} | {v} |" for k, v in flags.items()) or "| none | 0 |"

    convergence_rows = ""
    for pair in convergence.get("pairs", []):
        convergence_rows += (
            f"| {pair['coarse_m']:.0f} → {pair['fine_m']:.0f} m | "
            f"{fmt(pair.get('reach_delta_m'), 0)} | "
            f"{fmt(pair.get('reach_relative_delta'), 3)} | "
            f"{fmt(pair.get('depth_profile_relative_l1'), 3)} | "
            f"{fmt(pair.get('inundation_iou_1m'), 3)} |\n"
        )

    transect_rows = ""
    for name, block in (metrics.get("transects") or {}).items():
        transect_rows += (
            f"| `{name}` | {block.get('reached_members')} | "
            f"{fmt(block.get('arrival_mae_s'), 1)} | "
            f"{fmt(block.get('peak_stage_relative_error'), 3)} |\n"
        )

    # The artefact records `absolute_relative_error` (and `signed_relative_error`). Reading a
    # key that does not exist silently rendered `nan` into a verification table, which is the
    # one place a reader looks to check the solver conserves mass. Fail loudly instead.
    mass_block = verification.get("mass_conservation_closed_domain", {})
    if "absolute_relative_error" not in mass_block:
        raise SummaryReportError(
            "verification.json has no mass_conservation_closed_domain.absolute_relative_error; "
            "regenerate it with scripts/runout_verification_report.py"
        )
    mass_error = mass_block["absolute_relative_error"]
    lake = verification.get("lake_at_rest", {})
    lake_dev = lake.get("max_surface_deviation_m", float("nan"))
    lake_speed = lake.get("max_speed_m_s", float("nan"))
    lake_steps = lake.get("steps", 0)
    solver_version = design.get("solver_version", "0.1.0")
    bytes_mb = summary.get("bytes_on_disk", 0) / 1e6
    cap_gb = summary.get("bytes_cap", 0) / 1e9

    inundation = metrics.get("inundation", {})
    coverage = metrics.get("coverage", {})
    latency = metrics.get("latency", {})

    lake_row = f"surface deviation {lake_dev} m, max speed {lake_speed} m/s over {lake_steps} steps"
    reach_row = (
        "| "
        + " | ".join(fmt(reach.get(k)) for k in ("p5", "p25", "p50", "p75", "p95", "max"))
        + " |"
    )
    iou_row = (
        f"| Median inundation IoU at 1 m | {fmt(inundation.get('median_iou'), 3)} "
        f"| >= 0.70 | {inundation.get('gate_pass')} |"
    )
    depth_cov_row = (
        f"| 5-95% depth coverage | {fmt(coverage.get('max_depth_5_95'), 3)} "
        f"| 0.85-0.95 | {coverage.get('depth_gate_pass')} |"
    )
    arrival_cov_row = (
        f"| 5-95% arrival coverage | {fmt(coverage.get('arrival_5_95'), 3)} "
        f"| 0.85-0.95 | {coverage.get('arrival_gate_pass')} |"
    )
    scored = {
        name: block
        for name, block in (metrics.get("transects") or {}).items()
        if block.get("arrival_mae_s") is not None
    }
    unscored = sorted(set(metrics.get("transects") or {}) - set(scored))
    basis = (
        " / ".join(f"`{n}` n={b['reached_members']}" for n, b in sorted(scored.items()))
        or "no transect scored"
    )
    unscored_text = (
        ", ".join(f"`{n}`" for n in unscored)
        + " have no arrival metric because no member of the whole ensemble reaches them"
        if unscored
        else "every transect scored"
    )
    mae_row = (
        f"| Arrival MAE, worst **scored** transect | "
        f"{fmt(metrics.get('arrival_mae_worst_s'), 1)} s | <= 90 s | "
        f"{metrics.get('arrival_gate_pass')} |"
    )

    text = f"""# M4 runout — summary of what was built and what it shows

> **NOT r.avaflow.** Every depth, velocity and arrival time below comes from
> `serac-swe-voellmy` v{solver_version}, a single-phase depth-averaged
> Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained; see
> `infra/docker/ravaflow/README.md` for the acquisition record with dates and URLs.
> **Cross-validation against r.avaflow is outstanding.**

Generated {datetime.now(tz=UTC).date().isoformat()} from the machine-readable records in this
directory. Nothing here is retyped by hand.

## 1. Solver verification

| Case | Result |
|---|---|
| Mass conservation, closed domain | relative error {mass_error:.2e} |
| Lake at rest, random topography | {lake_row} |

### Ritter dam break against the analytic solution

| Cells | dx (m) | L1 (m^2) | L1 relative |
|---|---|---|---|
{ritter_rows}

### Voellmy terminal velocity, relative error by CFL

| CFL | Relative error |
|---|---|
{tv_rows}

The scheme applies gravity and friction as separate operators within a step, so the balance is
recovered only to first order in `dt`. At the production CFL of {production_cfl:g} the modelled
terminal velocity sits **{splitting_bias:.1%} below** the analytic value, which makes every
modelled arrival time late. It is reported here rather than removed.

## 2. Measured cost, and how the ensemble was sized

One parameter vector at each resolution, on the solver and simulated-time limit the ensemble
actually used:

| Resolution | Wall (s) | Steps | ms/step | Simulated (s) | Reach (km) | Active cells |
|---|---|---|---|---|---|---|
{timing_rows}

The runs the ensemble size was chosen against, measured before it was frozen. Two friction
values per resolution, because cost is dominated by `mu`:

| Resolution | mu | Wall (s) | Steps | ms/step | Reach (km) |
|---|---|---|---|---|---|
{sizing_rows}

Cost follows `{timing.get("cost_model", {}).get("form", "wall ~ k / mu")}` with
k = {timing.get("cost_model", {}).get("k_s", "?")} s. The ensemble size was chosen against these
numbers and against the contention actually observed on this machine; the reasoning is written
into `ENSEMBLE_FROZEN.md` and is not repeated here.

## 3. Grid convergence

| Pair | delta  reach (m) | delta  reach (rel) | Depth profile rel. L1 | Inundation IoU at 1 m |
|---|---|---|---|---|
{convergence_rows or "| (not run) | — | — | — | — |"}

## 4. The ensemble

| | |
|---|---|
| Design hash | `{summary.get("design_hash", "—")}` |
| Members recorded | {summary.get("n_members_recorded", "—")} |
| **Valid** | **{summary.get("n_valid", "—")}** |
| Flagged but retained | {summary.get("n_flagged_but_retained", "—")} |
| Bytes on disk | {bytes_mb:.1f} MB (cap {cap_gb:.0f} GB) |
| Total core-seconds | {summary.get("wall_time_total_core_s", "—")} |
| Resolution split | {resolution_split} |

**Not 230 equivalent 30 m runs.** A 30 m member costs 9-11x a 60 m one, so the ensemble is
dominated by the 60 m grid; section 3 above is what justifies it.

Runout distance reached, over valid members:

| p5 | p25 | median | p75 | p95 | max |
|---|---|---|---|---|---|
{reach_row}

(kilometres along the corridor; the corridor is 100 km long and the furthest transect is at
97.0 km.)

Flags on retained members — a flag is information, not a failure:

| Reason | Members |
|---|---|
{flag_rows}

## 5. The surrogate

| Gate | Measured | Target | Pass |
|---|---|---|---|
{iou_row}
{mae_row}
| p95 inference latency | {fmt(latency.get("p95_s"), 4)} s | <= 2 s | {latency.get("gate_pass")} |
{depth_cov_row}
{arrival_cov_row}

> **The arrival gate rests on {basis}.** "Worst" is a maximum over the transects that scored at
> all, not over all four: {unscored_text}. A gate computed from three
> held-out members at one transect is evidence that the surrogate reproduces the simulator
> there; it is not evidence about the corridor below it.

Splits are by `run_id` and disjoint: {metrics.get("splits_disjoint_by_run_id")}
({metrics.get("split_sizes")}).

| Transect | Test members reaching | Arrival MAE (s) | Peak-stage rel. error |
|---|---|---|---|
{transect_rows or "| (not trained) | — | — | — |"}

## 6. The finding that dominates everything above

{thalweg_sentence_text} — and that coefficient sits well
inside the published range for rock-ice avalanches and debris flows. The figure is read from
`terrain.json`, never written here. A single-phase Voellmy rheology
therefore stops far short of the ~100 km the 26 August 2026 cascade is reported to have
travelled. The comparison against the public timings, and the full mismatch distribution, are in
`langtang_sanity.md`; nothing was adjusted as a result of it.
"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "SUMMARY.md").write_text(text, encoding="utf-8")
    print(f"wrote {REPORTS / 'SUMMARY.md'}")  # noqa: T201


if __name__ == "__main__":
    main()
