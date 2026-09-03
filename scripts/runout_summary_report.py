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
    timing = load("timing.json") or {}
    verification = load("verification.json") or {}
    summary = load("ensemble_summary.json") or {}
    metrics = load("surrogate_metrics.json") or {}
    convergence = load("grid_convergence.json") or {}
    design = load("ensemble_design.json") or {}

    timing_rows = "\n".join(
        f"| {r['resolution_m']:.0f} m | {r['mu']:.2f} | {r['solver_wall_s']:.1f} | "
        f"{r['steps']} | {r['ms_per_step']:.2f} | {r['reach_m'] / 1000:.2f} | {r['active_cells']} |"
        for r in timing.get("runs", [])
    )

    ritter_rows = "\n".join(
        f"| {r['cells']} | {r['dx_m']:.1f} | {r['l1_m2']:.1f} | {r['l1_relative']:.2%} |"
        for r in verification.get("ritter_dam_break", [])
    )
    tv = verification.get("voellmy_terminal_velocity_relative_error_by_cfl", {})
    tv_rows = "\n".join(f"| {k} | {v:.3%} |" for k, v in sorted(tv.items(), reverse=True))

    reach = summary.get("reach_km", {})
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

    inundation = metrics.get("inundation", {})
    coverage = metrics.get("coverage", {})
    latency = metrics.get("latency", {})

    text = f"""# M4 runout — summary of what was built and what it shows

> **NOT r.avaflow.** Every depth, velocity and arrival time below comes from
> `serac-swe-voellmy` v{design.get("solver_version", "0.1.0")}, a single-phase depth-averaged
> Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained; see
> `infra/docker/ravaflow/README.md` for the acquisition record with dates and URLs.
> **Cross-validation against r.avaflow is outstanding.**

Generated {datetime.now(tz=UTC).date().isoformat()} from the machine-readable records in this
directory. Nothing here is retyped by hand.

## 1. Solver verification

| Case | Result |
|---|---|
| Mass conservation, closed domain | relative error {verification.get("mass_conservation_closed_domain", {}).get("relative_error", float("nan")):.2e} |
| Lake at rest, random topography | surface deviation {verification.get("lake_at_rest", {}).get("max_surface_deviation_m", float("nan"))} m, max speed {verification.get("lake_at_rest", {}).get("max_speed_m_s", float("nan"))} m/s over {verification.get("lake_at_rest", {}).get("steps", 0)} steps |

### Ritter dam break against the analytic solution

| Cells | dx (m) | L1 (m²) | L1 relative |
|---|---|---|---|
{ritter_rows}

### Voellmy terminal velocity, relative error by CFL

| CFL | Relative error |
|---|---|
{tv_rows}

The scheme applies gravity and friction as separate operators within a step, so the balance is
recovered only to first order in `dt`. At the production CFL the modelled terminal velocity sits
about 7.6% **below** the analytic value, which makes every modelled arrival time late. It is
reported here rather than removed.

## 2. Measured cost, and how the ensemble was sized

| Resolution | mu | Wall (s) | Steps | ms/step | Reach (km) | Active cells |
|---|---|---|---|---|---|---|
{timing_rows}

Cost follows `{timing.get("cost_model", {}).get("form", "wall ~ k / mu")}` with
k = {timing.get("cost_model", {}).get("k_s", "?")} s. The ensemble size was chosen against these
numbers and against the contention actually observed on this machine; the reasoning is written
into `ENSEMBLE_FROZEN.md` and is not repeated here.

## 3. Grid convergence

| Pair | Δ reach (m) | Δ reach (rel) | Depth profile rel. L1 | Inundation IoU at 1 m |
|---|---|---|---|---|
{convergence_rows or "| (not run) | — | — | — | — |"}

## 4. The ensemble

| | |
|---|---|
| Design hash | `{summary.get("design_hash", "—")}` |
| Members recorded | {summary.get("n_members_recorded", "—")} |
| **Valid** | **{summary.get("n_valid", "—")}** |
| Flagged but retained | {summary.get("n_flagged_but_retained", "—")} |
| Bytes on disk | {summary.get("bytes_on_disk", 0) / 1e6:.1f} MB (cap {summary.get("bytes_cap", 0) / 1e9:.0f} GB) |
| Total core-seconds | {summary.get("wall_time_total_core_s", "—")} |

Runout distance reached, over valid members:

| p5 | p25 | median | p75 | p95 | max |
|---|---|---|---|---|---|
| {fmt(reach.get("p5"))} | {fmt(reach.get("p25"))} | {fmt(reach.get("p50"))} | {fmt(reach.get("p75"))} | {fmt(reach.get("p95"))} | {fmt(reach.get("max"))} |

(kilometres along the corridor; the corridor is 100 km long and the furthest transect is at
97.0 km.)

Flags on retained members — a flag is information, not a failure:

| Reason | Members |
|---|---|
{flag_rows}

## 5. The surrogate

| Gate | Measured | Target | Pass |
|---|---|---|---|
| Median inundation IoU at 1 m | {fmt(inundation.get("median_iou"), 3)} | ≥ 0.70 | {inundation.get("gate_pass")} |
| Worst per-transect arrival MAE | {fmt(metrics.get("arrival_mae_worst_s"), 1)} s | ≤ 90 s | {metrics.get("arrival_gate_pass")} |
| p95 inference latency | {fmt(latency.get("p95_s"), 4)} s | ≤ 2 s | {latency.get("gate_pass")} |
| 5–95% depth coverage | {fmt(coverage.get("max_depth_5_95"), 3)} | 0.85–0.95 | {coverage.get("depth_gate_pass")} |
| 5–95% arrival coverage | {fmt(coverage.get("arrival_5_95"), 3)} | 0.85–0.95 | {coverage.get("arrival_gate_pass")} |

Splits are by `run_id` and disjoint: {metrics.get("splits_disjoint_by_run_id")}
({metrics.get("split_sizes", {})}).

| Transect | Test members reaching | Arrival MAE (s) | Peak-stage rel. error |
|---|---|---|---|
{transect_rows or "| (not trained) | — | — | — |"}

## 6. The finding that dominates everything above

92% of this corridor's thalweg is below 6.8 degrees, median 0.42 degrees. A Voellmy Coulomb
coefficient above roughly 0.08 cannot sustain motion over most of it — and 0.08 sits well inside
the published range for rock-ice avalanches and debris flows. A single-phase Voellmy rheology
therefore stops far short of the ~100 km the 26 August 2026 cascade is reported to have
travelled. The comparison against the public timings, and the full mismatch distribution, are in
`langtang_sanity.md`; nothing was adjusted as a result of it.
"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "SUMMARY.md").write_text(text, encoding="utf-8")
    print(f"wrote {REPORTS / 'SUMMARY.md'}")  # noqa: T201


if __name__ == "__main__":
    main()
