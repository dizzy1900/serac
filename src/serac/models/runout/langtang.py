"""The Langtang sanity check: find the closest member, report the mismatch, change nothing.

This is **not** calibration, tuning or fitting, and `validate-runout` runs a forbidden-vocabulary
grep over `reports/runout/*.md` so the write-up cannot describe it as any of those. The ensemble
design is frozen before this runs, its hash is asserted here, and no parameter is adjusted
afterwards. What the check does is:

1. take the four publicly reported timings for the 26 August 2026 cascade;
2. find the ensemble member whose modelled arrivals are closest to them;
3. report the **full distribution** of mismatch across every member, not just the best one;
4. state plainly which transects no member reaches at all.

The public timings are press-attributed figures for an event with no peer-reviewed source as of
September 2026. They are quoted here as the comparison target and are **not** used to select,
weight, filter or adjust anything. If the closest member is far off, that is the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from serac.models.runout.corridor import load_frame, transect_chainages
from serac.models.runout.driver import INDEX_FILENAME, iter_index
from serac.models.runout.ensemble import design_from_payload, read_frozen_design
from serac.models.runout.params import (
    NOT_RAVAFLOW,
    RESOLUTION_LIMITATION,
    SINGLE_PHASE_LIMITATION,
    SOLVER_NAME,
    SOLVER_VERSION,
)
from serac.models.runout.release import RELEASE_AT_REST_ASSUMPTION
from serac.models.runout.terrain import thalweg_sentence, thalweg_statistics

SANITY_FILENAME = "langtang_sanity.md"
SANITY_JSON = "langtang_sanity.json"

PUBLIC_TIMINGS_MIN: dict[str, float] = {
    "rasuwagadhi-gyirong": 7.5,
    "syabrubesi": 13.5,
    "betrawati": 45.0,
    "galchhi": 30.0,
}
"""Minutes after the detachment, as publicly reported. `syabrubesi` is the midpoint of the
reported 13-14 min; `galchhi` is the reported "+9 m in 30 min" stage rise. These are press
figures for an event with no peer-reviewed source as of September 2026 (see
`data/events/langtang-lhende-2026.json`, where the same figures carry `best: null`)."""

FORBIDDEN_VOCABULARY: tuple[str, ...] = (
    "calibrat",
    "calibration",
    "tuned",
    "tuning",
    "fitted",
    "fitting",
    "fit to",
    "best fit",
    "best-fit",
    "matched to",
    "history match",
    "history-match",
)
"""Grepped over the M4 write-ups by `validate-runout`. If any appears, the gate fails.

Bare `fitted` and `fitting` are in the list, not just `fitted to`: a mutation check found that
"parameters fitted the arrivals" slipped through a list that only held the two-word forms. The
rule is deliberately blunt -- a sentence that needs one of these words to say what it means is a
sentence a reviewer should see."""


@dataclass(frozen=True)
class MemberMismatch:
    run_id: str
    modelled_min: dict[str, float | None]
    mismatch_min: dict[str, float | None]
    reached_count: int
    mean_abs_mismatch_min: float | None
    parameters: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "modelled_arrival_min": self.modelled_min,
            "mismatch_min": self.mismatch_min,
            "transects_reached": self.reached_count,
            "mean_abs_mismatch_min": self.mean_abs_mismatch_min,
            "parameters": self.parameters,
        }


def _splitting_bias(reports_dir: Path) -> dict[str, float]:
    """The production-CFL terminal-velocity bias, read from the committed verification record.

    Read rather than hardcoded: earlier drafts quoted the CFL 0.4 figure as though it were the
    production one, understating the model's own late-arrival bias.
    """
    path = reports_dir / "verification.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run scripts/runout_verification_report.py")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        "production_cfl": float(doc["production_cfl"]),
        "terminal_velocity_bias_at_production_cfl": float(
            doc["voellmy_terminal_velocity_at_production_cfl"]
        ),
    }


def collect_mismatches(
    repo: Path, reports_dir: Path, aoi_id: str = "lhende-khola-trishuli"
) -> list[MemberMismatch]:
    """Each valid member's transect arrivals and their distance from the public figures."""
    frame = load_frame(repo / "data" / "aoi" / aoi_id, 32645)
    transects = transect_chainages(repo / "data" / "aoi" / aoi_id, frame)
    index_path = reports_dir / INDEX_FILENAME
    out: list[MemberMismatch] = []
    for row in iter_index(index_path):
        if not row.get("valid"):
            continue
        directory = (
            repo / row["directory"]
            if not Path(row["directory"]).is_absolute()
            else Path(row["directory"])
        )
        parquet = directory / "corridor.parquet"
        if not parquet.exists():
            continue
        table = pq.read_table(parquet)
        chainage = np.asarray(table["chainage_m"], dtype=np.float64)
        arrival = np.asarray(table["arrival_time_s"], dtype=np.float64)
        modelled: dict[str, float | None] = {}
        mismatch: dict[str, float | None] = {}
        for transect in transects:
            idx = int(np.argmin(np.abs(chainage - transect.frame_chainage_m)))
            value = arrival[idx]
            if np.isfinite(value):
                minutes = float(value) / 60.0
                modelled[transect.transect_id] = round(minutes, 2)
                target = PUBLIC_TIMINGS_MIN.get(transect.transect_id)
                mismatch[transect.transect_id] = (
                    round(minutes - target, 2) if target is not None else None
                )
            else:
                modelled[transect.transect_id] = None
                mismatch[transect.transect_id] = None
        deltas = [abs(v) for v in mismatch.values() if v is not None]
        out.append(
            MemberMismatch(
                run_id=row["run_id"],
                modelled_min=modelled,
                mismatch_min=mismatch,
                reached_count=len(deltas),
                mean_abs_mismatch_min=round(float(np.mean(deltas)), 3) if deltas else None,
                parameters=row.get("parameters", {}),
            )
        )
    return out


def closest_member(mismatches: list[MemberMismatch]) -> MemberMismatch | None:
    """The member that reaches the most transects and, among those, is closest on average.

    Reach comes first deliberately: a member that reaches one transect within a minute is not
    "closer to the observation" than one that reaches three within five minutes, and ranking on
    mean error alone would prefer the former.
    """
    scored = [m for m in mismatches if m.mean_abs_mismatch_min is not None]
    if not scored:
        return None
    return min(scored, key=lambda m: (-m.reached_count, m.mean_abs_mismatch_min or 1e9))


def write_sanity_check(
    repo: Path, *, aoi_id: str = "lhende-khola-trishuli", reports_dir: Path | None = None
) -> Path:
    """Write `langtang_sanity.md` and its JSON companion."""
    reports = reports_dir or (repo / "reports" / "runout")
    reports.mkdir(parents=True, exist_ok=True)
    mismatches = collect_mismatches(repo, reports, aoi_id)
    best = closest_member(mismatches)
    design = design_from_payload(read_frozen_design(reports))

    per_transect: dict[str, Any] = {}
    for name, target in PUBLIC_TIMINGS_MIN.items():
        values = [m.mismatch_min.get(name) for m in mismatches]
        finite = np.array([v for v in values if v is not None], dtype=np.float64)
        per_transect[name] = {
            "public_timing_min": target,
            "members_reaching": int(finite.size),
            "members_total": len(mismatches),
            "fraction_reaching": round(finite.size / max(len(mismatches), 1), 4),
            "mismatch_min": (
                {
                    "min": round(float(finite.min()), 2),
                    "p25": round(float(np.percentile(finite, 25)), 2),
                    "median": round(float(np.median(finite)), 2),
                    "p75": round(float(np.percentile(finite, 75)), 2),
                    "max": round(float(finite.max()), 2),
                    "closest_absolute": round(float(np.abs(finite).min()), 2),
                }
                if finite.size
                else None
            ),
        }

    payload = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "thalweg_sentence": thalweg_sentence(reports),
        "thalweg_source": "measured, reports/runout/terrain.json",
        "thalweg_statistics": thalweg_statistics(reports),
        **_splitting_bias(reports),
        "solver": {"name": SOLVER_NAME, "version": SOLVER_VERSION},
        "frozen_design_hash": design.design_hash,
        "frozen_solver_version": design.payload["solver_version"],
        "public_timings_min": PUBLIC_TIMINGS_MIN,
        "n_members": len(mismatches),
        "per_transect": per_transect,
        "closest_member": best.as_dict() if best else None,
        "all_members": [m.as_dict() for m in mismatches],
    }
    (reports / SANITY_JSON).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    path = reports / SANITY_FILENAME
    path.write_text(_render(payload), encoding="utf-8")
    return path


def _render(payload: dict[str, Any]) -> str:
    thalweg = payload["thalweg_sentence"]
    thalweg_source = payload["thalweg_source"]
    production_cfl = payload["production_cfl"]
    splitting_bias = payload["terminal_velocity_bias_at_production_cfl"]
    best = payload["closest_member"]
    rows = []
    for name, block in payload["per_transect"].items():
        stats = block["mismatch_min"]
        if stats is None:
            rows.append(
                f"| `{name}` | {block['public_timing_min']:.1f} | "
                f"0 / {block['members_total']} | not reached by any member | — | — |"
            )
            continue
        rows.append(
            f"| `{name}` | {block['public_timing_min']:.1f} | "
            f"{block['members_reaching']} / {block['members_total']} | "
            f"{stats['min']:+.2f} to {stats['max']:+.2f} | {stats['median']:+.2f} | "
            f"{stats['closest_absolute']:.2f} |"
        )

    if best is None:
        best_block = (
            "**No member reached any transect**, so there is no closest member to report. "
            "The comparison result is that the ensemble does not produce a flow that arrives "
            "at any of the four transects."
        )
    else:
        lines = [
            f"| `{k}` | "
            + (f"{v:.2f}" if v is not None else "not reached")
            + " | "
            + (
                f"{best['mismatch_min'][k]:+.2f}"
                if best["mismatch_min"].get(k) is not None
                else "—"
            )
            + " |"
            for k, v in best["modelled_arrival_min"].items()
        ]
        params = best["parameters"]
        reached = best["transects_reached"]
        best_block = f"""Run `{best["run_id"]}`, which reached {reached} of the four transects
with a mean absolute mismatch of {best["mean_abs_mismatch_min"]} minutes.

| Transect | Modelled arrival (min) | Mismatch (min) |
|---|---|---|
{chr(10).join(lines)}

Its parameters, for the record and for no other purpose:

```json
{json.dumps(params, indent=2, sort_keys=True)}
```"""

    return f"""# Langtang 2026 — comparison against public timings

> {NOT_RAVAFLOW}

**This is a comparison, not an adjustment.** The ensemble design was frozen before this
comparison ran (design hash `{payload["frozen_design_hash"]}`, solver
`{payload["frozen_solver_version"]}`), no parameter was changed as a result of it, and no member
was selected, weighted or removed on the basis of these numbers. `validate-runout` greps this
file for the vocabulary that would describe it otherwise.

## The public figures

The four timings below are **press-attributed** figures for an event with no peer-reviewed
source as of September 2026. In `data/events/langtang-lhende-2026.json` the corresponding
fields carry `best: null` for exactly that reason. They are quoted here as the comparison
target.

| Transect | Public timing (min after detachment) |
|---|---|
| `rasuwagadhi-gyirong` | ~7.5 |
| `syabrubesi` | ~13-14 (midpoint 13.5 used) |
| `betrawati` | ~45 |
| `galchhi` | +9 m stage in ~30 |

## Mismatch across the whole ensemble

Positive means the model arrives **later** than the public figure.

| Transect | Public | Reaching | Mismatch range (min) | Median | Closest abs |
|---|---|---|---|---|---|
{chr(10).join(rows)}

## Closest member

{best_block}

## What the mismatch is telling you

Three structural properties of the model bear directly on these numbers, and all three were
known before the comparison ran:

1. **{RELEASE_AT_REST_ASSUMPTION}** Arrival times are therefore biased late at every transect.
2. **{SINGLE_PHASE_LIMITATION}** The observed cascade travelled roughly 100 km, which a
   water-dominated flood wave does readily and a Coulomb-plus-turbulent avalanche rheology does
   not: {thalweg}, whatever else is varied ({thalweg_source}).
3. **{RESOLUTION_LIMITATION}**

The operator-splitting error measured in `reports/runout/verification.json` adds a further
known bias: at the production CFL of {production_cfl:g} the modelled terminal velocity sits
{splitting_bias:.1%} below the analytic Voellmy value, which makes arrivals later still.

r.avaflow cross-validation remains outstanding, so there is no independent simulator against
which to separate these structural biases from implementation error.
"""
