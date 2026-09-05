"""The Langtang sanity check: find the closest member, report the mismatch, change nothing.

This is **not** calibration, tuning or fitting, and `validate-runout` runs a forbidden-vocabulary
grep over `reports/runout/*.md` so the write-up cannot describe it as any of those. The ensemble
design is frozen before this runs, its hash is asserted here, and no parameter is adjusted
afterwards. What the check does is:

1. read the transect arrival times the **event record** holds for the 26 August 2026 cascade
   (`serac.models.runout.observed`), and only those;
2. find the ensemble member whose modelled arrivals are closest to them;
3. report the **full distribution** of mismatch across every member, not just the best one;
4. state plainly which transects no member reaches at all, and which transects the record holds
   no arrival time for, so that no comparison against them is possible.

Where the comparison targets come from
--------------------------------------
Nowhere in this module. `data/events/langtang-lhende-2026.json` is the only source of an
observed figure here, and it holds an `arrival_time_min` for exactly one transect
(`syabrubesi`, 13 min, the difference between two clock times the Kathmandu Post states, press
only, `best: null`). Rasuwagadhi and Betrawati carry `arrival_time_min: null` because the ~7.5
and ~45 min figures that circulate publicly have no retrievable source; Galchhi carries a +9 m
stage rise over 30 minutes, which is a stage-rise window and not an arrival time. An earlier
version of this module held all four as a `PUBLIC_TIMINGS_MIN` literal and the write-up called
them press-attributed — see `serac.models.runout.observed` for why the literal is gone rather
than corrected.

The recorded figure is quoted as the comparison target and is **not** used to select, weight,
filter or adjust anything. If the closest member is far off, that is the result.
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
from serac.models.runout.observed import (
    TARGET_EVENT_ID,
    ObservedTimingError,
    TransectTarget,
    comparison_targets,
    load_payload,
    load_transect_targets,
    record_path,
)
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

MEMBERS_FROM_RASTERS = "corridor.parquet per member (data/interim/, DVC-tracked)"
MEMBERS_FROM_ARTIFACT = (
    f"modelled_arrival_min recorded in reports/runout/{SANITY_JSON} "
    "(the per-member rasters were not on disk; no modelled number was recomputed)"
)
"""Provenance strings for the modelled arrivals a payload was built from. One of the two is
recorded in every artifact, because "where did these arrivals come from" is a question a
reader of the report must be able to answer without reading the code."""

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
class MemberArrivals:
    """One ensemble member's modelled arrival time at each transect. Solver output only."""

    run_id: str
    modelled_min: dict[str, float | None]
    parameters: dict[str, Any]


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


def collect_member_arrivals(
    repo: Path, reports_dir: Path, aoi_id: str = "lhende-khola-trishuli"
) -> tuple[list[MemberArrivals], list[str]]:
    """Each valid member's modelled transect arrivals, read from its corridor raster.

    Returns the members and the AOI's transect ids in chainage order. No observation is read
    here: this is solver output and nothing else.
    """
    frame = load_frame(repo / "data" / "aoi" / aoi_id, 32645)
    transects = transect_chainages(repo / "data" / "aoi" / aoi_id, frame)
    index_path = reports_dir / INDEX_FILENAME
    out: list[MemberArrivals] = []
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
        for transect in transects:
            idx = int(np.argmin(np.abs(chainage - transect.frame_chainage_m)))
            value = arrival[idx]
            modelled[transect.transect_id] = (
                round(float(value) / 60.0, 2) if np.isfinite(value) else None
            )
        out.append(
            MemberArrivals(
                run_id=row["run_id"], modelled_min=modelled, parameters=row.get("parameters", {})
            )
        )
    return out, [t.transect_id for t in transects]


def member_arrivals_from_artifact(path: Path) -> tuple[list[MemberArrivals], list[str]]:
    """The modelled arrivals already recorded in a `langtang_sanity.json`.

    The per-member rasters live under `data/interim/` (DVC-tracked, gitignored), so a clone
    without them cannot re-read `corridor.parquet`. The modelled arrivals are the solver's own
    output and are committed inside the artifact, so the comparison can be re-derived against
    the event record without re-running or recomputing a single modelled number. The rebuilt
    payload records `MEMBERS_FROM_ARTIFACT` as its provenance so a reader can tell which path
    produced it.
    """
    doc = load_payload(path)
    members: list[MemberArrivals] = []
    ordered: list[str] = []
    for entry in doc.get("all_members", []):
        arrivals = entry.get("modelled_arrival_min") if isinstance(entry, dict) else None
        if not isinstance(arrivals, dict):
            raise ObservedTimingError(f"{path}: a member carries no modelled_arrival_min block")
        modelled: dict[str, float | None] = {}
        for key, value in arrivals.items():
            if key not in ordered:
                ordered.append(str(key))
            modelled[str(key)] = None if value is None else float(value)
        members.append(
            MemberArrivals(
                run_id=str(entry["run_id"]),
                modelled_min=modelled,
                parameters=entry.get("parameters", {}),
            )
        )
    if not members:
        raise ObservedTimingError(f"{path}: no members recorded, nothing to re-derive")
    return members, ordered


def compare(
    members: list[MemberArrivals], targets: tuple[TransectTarget, ...]
) -> list[MemberMismatch]:
    """Signed mismatch per member, computed only where the event record holds an arrival time."""
    recorded = comparison_targets(targets)
    out: list[MemberMismatch] = []
    for member in members:
        mismatch: dict[str, float | None] = {}
        for transect_id, minutes in member.modelled_min.items():
            target = recorded.get(transect_id)
            mismatch[transect_id] = (
                round(target.signed_gap_min(minutes), 2)
                if target is not None and minutes is not None
                else None
            )
        deltas = [abs(v) for v in mismatch.values() if v is not None]
        out.append(
            MemberMismatch(
                run_id=member.run_id,
                modelled_min=dict(member.modelled_min),
                mismatch_min=mismatch,
                reached_count=len(deltas),
                mean_abs_mismatch_min=round(float(np.mean(deltas)), 3) if deltas else None,
                parameters=member.parameters,
            )
        )
    return out


def closest_member(mismatches: list[MemberMismatch]) -> MemberMismatch | None:
    """The member that reaches the most compared transects and is closest on average.

    Reach comes first deliberately: a member that reaches one transect within a minute is not
    "closer to the observation" than one that reaches three within five minutes, and ranking on
    mean error alone would prefer the former. "Compared transects" means the ones the event
    record holds an arrival time for; a member that reaches a transect with no recorded arrival
    has produced no mismatch there and cannot be ranked on it.
    """
    scored = [m for m in mismatches if m.mean_abs_mismatch_min is not None]
    if not scored:
        return None
    return min(scored, key=lambda m: (-m.reached_count, m.mean_abs_mismatch_min or 1e9))


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype=np.float64)
    return {
        "min": round(float(array.min()), 2),
        "p25": round(float(np.percentile(array, 25)), 2),
        "median": round(float(np.median(array)), 2),
        "p75": round(float(np.percentile(array, 75)), 2),
        "max": round(float(array.max()), 2),
        "closest_absolute": round(float(np.abs(array).min()), 2),
    }


def build_payload(
    *,
    mismatches: list[MemberMismatch],
    targets: tuple[TransectTarget, ...],
    transect_ids: list[str],
    reports_dir: Path,
    members_source: str,
    event_id: str = TARGET_EVENT_ID,
) -> dict[str, Any]:
    """Assemble the artifact. Every observed figure in it comes from `targets`."""
    design = design_from_payload(read_frozen_design(reports_dir))
    by_id = {t.transect_id: t for t in targets}

    per_transect: dict[str, Any] = {}
    for transect_id in transect_ids:
        target = by_id.get(transect_id)
        modelled = [
            m.modelled_min[transect_id]
            for m in mismatches
            if m.modelled_min.get(transect_id) is not None
        ]
        gaps = [
            m.mismatch_min[transect_id]
            for m in mismatches
            if m.mismatch_min.get(transect_id) is not None
        ]
        per_transect[transect_id] = {
            "recorded_arrival_min": (
                {
                    "low": target.arrival_low_min,
                    "high": target.arrival_high_min,
                    "best": target.arrival_best_min,
                    "unit": "min",
                    "source_refs": list(target.arrival_source_refs),
                }
                if target is not None and target.is_comparison_target
                else None
            ),
            "is_comparison_target": bool(target is not None and target.is_comparison_target),
            "no_recorded_arrival_reason": (
                None
                if target is not None and target.is_comparison_target
                else (
                    target.absent_reason
                    if target is not None
                    else f"the {event_id} record holds no observation for this transect"
                )
            ),
            "members_reaching": len(modelled),
            "members_total": len(mismatches),
            "fraction_reaching": round(len(modelled) / max(len(mismatches), 1), 4),
            "modelled_arrival_min": _distribution([float(v) for v in modelled if v is not None]),
            "mismatch_min": _distribution([float(v) for v in gaps if v is not None]),
        }

    best = closest_member(mismatches)
    return {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "thalweg_sentence": thalweg_sentence(reports_dir),
        "thalweg_source": "measured, reports/runout/terrain.json",
        "thalweg_statistics": thalweg_statistics(reports_dir),
        **_splitting_bias(reports_dir),
        "solver": {"name": SOLVER_NAME, "version": SOLVER_VERSION},
        "frozen_design_hash": design.design_hash,
        "frozen_solver_version": design.payload["solver_version"],
        "observation_source": str(Path("data") / "events" / f"{event_id}.json"),
        "member_arrivals_source": members_source,
        "transect_targets": [t.as_dict() for t in targets],
        "n_comparison_targets": len(comparison_targets(targets)),
        "n_members": len(mismatches),
        "per_transect": per_transect,
        "closest_member": best.as_dict() if best else None,
        "all_members": [m.as_dict() for m in mismatches],
    }


def write_sanity_check(
    repo: Path,
    *,
    aoi_id: str = "lhende-khola-trishuli",
    reports_dir: Path | None = None,
    from_artifact: bool = False,
    event_id: str = TARGET_EVENT_ID,
) -> Path:
    """Write `langtang_sanity.md` and its JSON companion.

    `from_artifact` re-derives the comparison from the modelled arrivals already recorded in
    `langtang_sanity.json` instead of the per-member rasters (see `member_arrivals_from_artifact`).
    """
    reports = reports_dir or (repo / "reports" / "runout")
    reports.mkdir(parents=True, exist_ok=True)
    targets = load_transect_targets(repo, event_id)
    if from_artifact:
        members, transect_ids = member_arrivals_from_artifact(reports / SANITY_JSON)
        members_source = MEMBERS_FROM_ARTIFACT
    else:
        members, transect_ids = collect_member_arrivals(repo, reports, aoi_id)
        members_source = MEMBERS_FROM_RASTERS
    payload = build_payload(
        mismatches=compare(members, targets),
        targets=targets,
        transect_ids=transect_ids,
        reports_dir=reports,
        members_source=members_source,
        event_id=event_id,
    )
    (reports / SANITY_JSON).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    path = reports / SANITY_FILENAME
    path.write_text(render(payload), encoding="utf-8")
    return path


def _recorded_rows(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The two provenance tables: what the record holds, and what it explicitly does not."""
    held: list[str] = []
    absent: list[str] = []
    for entry in payload["transect_targets"]:
        transect_id = entry["transect_id"]
        if entry["is_comparison_target"]:
            low, high = entry["arrival_low_min"], entry["arrival_high_min"]
            span = f"{low:g}" if low == high else f"{low:g}-{high:g}"
            refs = ", ".join(f"`{r}`" for r in entry["arrival_source_refs"])
            best = "null" if entry["arrival_best_min"] is None else f"{entry['arrival_best_min']:g}"
            held.append(f"| `{transect_id}` | {span} | {best} | {refs} |")
        else:
            other = "; ".join(entry["other_observations"]) or "—"
            absent.append(f"| `{transect_id}` | {other} | {entry['absent_reason']} |")
    return held, absent


def _best_sentence(payload: dict[str, Any]) -> str:
    """What the record's `best` values are — read off the payload, never assumed."""
    targets = [t for t in payload["transect_targets"] if t["is_comparison_target"]]
    if not targets:
        return (
            "The record holds no arrival time at all, so there is nothing to compare against "
            "and every row below is model output."
        )
    with_best = [t["transect_id"] for t in targets if t["arrival_best_min"] is not None]
    if not with_best:
        return (
            "`best` is `null` for every one of them: the record carries the figure and its "
            "sources without asserting a preferred value, because no source qualifies to set "
            "one. The comparison uses the recorded `low`-`high` interval, not a midpoint of it."
        )
    named = ", ".join(f"`{t}`" for t in with_best)
    return (
        f"{named} carry a `best`; the comparison still uses the recorded `low`-`high` interval, "
        "not the `best` and not a midpoint."
    )


def _mismatch_rows(payload: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for name, block in payload["per_transect"].items():
        if not block["is_comparison_target"]:
            continue
        recorded = block["recorded_arrival_min"]
        low, high = recorded["low"], recorded["high"]
        span = f"{low:g}" if low == high else f"{low:g}-{high:g}"
        stats = block["mismatch_min"]
        if stats is None:
            rows.append(
                f"| `{name}` | {span} | 0 / {block['members_total']} | "
                "not reached by any member | — | — |"
            )
            continue
        rows.append(
            f"| `{name}` | {span} | {block['members_reaching']} / {block['members_total']} | "
            f"{stats['min']:+.2f} to {stats['max']:+.2f} | {stats['median']:+.2f} | "
            f"{stats['closest_absolute']:.2f} |"
        )
    return rows


def _modelled_rows(payload: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for name, block in payload["per_transect"].items():
        stats = block["modelled_arrival_min"]
        compared = "yes" if block["is_comparison_target"] else "no (no recorded arrival)"
        if stats is None:
            rows.append(
                f"| `{name}` | 0 / {block['members_total']} | not reached by any member | — | "
                f"{compared} |"
            )
            continue
        rows.append(
            f"| `{name}` | {block['members_reaching']} / {block['members_total']} | "
            f"{stats['min']:.2f} to {stats['max']:.2f} | {stats['median']:.2f} | {compared} |"
        )
    return rows


def _closest_block(payload: dict[str, Any]) -> str:
    best = payload["closest_member"]
    compared = [
        name for name, block in payload["per_transect"].items() if block["is_comparison_target"]
    ]
    named = ", ".join(f"`{c}`" for c in compared) or "none"
    if best is None:
        return (
            f"**No member reached a transect the event record holds an arrival time for** "
            f"({named}), so there is no closest member and no mismatch to report. A member that "
            "reaches a transect with no recorded arrival has produced nothing to compare: the "
            "modelled arrivals for those transects are in the table above, and they are the "
            "model's own output, not a match to an observation."
        )
    lines = [
        f"| `{k}` | "
        + (f"{v:.2f}" if v is not None else "not reached")
        + " | "
        + (
            f"{best['mismatch_min'][k]:+.2f}"
            if best["mismatch_min"].get(k) is not None
            else "— (no recorded arrival)"
        )
        + " |"
        for k, v in best["modelled_arrival_min"].items()
    ]
    params = json.dumps(best["parameters"], indent=2, sort_keys=True)
    return f"""Run `{best["run_id"]}`, which reached {best["transects_reached"]} of the
{payload["n_comparison_targets"]} transect(s) the record holds an arrival time for ({named}),
with a mean absolute mismatch of {best["mean_abs_mismatch_min"]} minutes.

| Transect | Modelled arrival (min) | Mismatch (min) |
|---|---|---|
{chr(10).join(lines)}

Its parameters, for the record and for no other purpose:

```json
{params}
```"""


def render(payload: dict[str, Any]) -> str:
    """The write-up, as a pure function of the artifact.

    `validate-runout` re-renders the committed payload and fails if the committed markdown
    differs, so the prose cannot come to say something the gated payload does not hold.
    """
    thalweg = payload["thalweg_sentence"]
    thalweg_source = payload["thalweg_source"]
    production_cfl = payload["production_cfl"]
    splitting_bias = payload["terminal_velocity_bias_at_production_cfl"]
    held, absent = _recorded_rows(payload)
    n_targets = payload["n_comparison_targets"]

    return f"""# Langtang 2026 — comparison against the recorded transect arrivals

> {NOT_RAVAFLOW}

**This is a comparison, not an adjustment.** The ensemble design was frozen before this
comparison ran (design hash `{payload["frozen_design_hash"]}`, solver
`{payload["frozen_solver_version"]}`), no parameter was changed as a result of it, and no member
was selected, weighted or removed on the basis of these numbers. `validate-runout` greps this
file for the vocabulary that would describe it otherwise.

## What the event record holds

Every observed figure below is read from `{payload["observation_source"]}` and none is written
in the code that produced this file. The record holds an arrival time for
**{n_targets} of {len(payload["transect_targets"])} transects**, and that is what the comparison
compares against. Modelled arrivals come from {payload["member_arrivals_source"]}.

| Transect | Recorded arrival (min after detachment) | `best` | Sources |
|---|---|---|---|
{chr(10).join(held) if held else "| — | — | — | — |"}

{_best_sentence(payload)}

### Transects with no recorded arrival time

These are **not** comparison targets. The event library examined the figures that circulate for
them and declined to record them; the reason below is the record's own sentence. Quoting one of
these numbers here as a target would be asserting a provenance the record refuses.

| Transect | What the record does hold | Why there is no arrival time |
|---|---|---|
{chr(10).join(absent) if absent else "| — | — | — |"}

## Modelled arrivals across the whole ensemble

Solver output over {payload["n_members"]} members. No observation enters this table.

| Transect | Reaching | Modelled arrival range (min) | Median | Compared |
|---|---|---|---|---|
{chr(10).join(_modelled_rows(payload))}

## Mismatch against the recorded arrivals

Positive means the model arrives **later** than the recorded interval; a modelled arrival inside
the recorded interval scores 0. Only the transects above with a recorded arrival appear here.

| Transect | Recorded | Reaching | Mismatch range (min) | Median | Closest abs |
|---|---|---|---|---|---|
{chr(10).join(_mismatch_rows(payload)) if _mismatch_rows(payload) else "| — | — | — | — | — | — |"}

## Closest member

{_closest_block(payload)}

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


def sanity_record_path(repo: Path, event_id: str = TARGET_EVENT_ID) -> Path:
    """The event record this comparison reads; re-exported so gates need one import."""
    return record_path(repo, event_id)
