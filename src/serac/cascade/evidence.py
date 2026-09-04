"""Read what M1, M2 and M4 actually produced, from their committed report artifacts.

The end-to-end lane needs each upstream stage's *as-run* outcome. Two ways to get one:

* **executed** -- the stage was run here, in this process, on committed fixtures. That is what
  `serac.pipelines.e2e` does for the M1 detector and (where the Green's-function fixtures
  cover it) for the M2 inversion.
* **artifact** -- the stage's own committed report is read. Necessary wherever a fresh clone
  has no `data/raw/` (DVC-tracked, gitignored): the discriminator training waveforms, the
  Chamoli Green's library and the runout ensemble's per-member rasters all live there.

Every `StageEvidence` says which of the two it is, names the file, and carries the file's
sha256, so a reader can tell "serac ran this just now" from "serac is quoting a run from
2026-09-03" without reading the code.

A guard on the M4 reader
------------------------
`reports/runout/langtang_sanity.json` exists to compare the ensemble with **press-reported**
timings. Reading a press figure into a forecast, or picking the member closest to one, would
be tuning toward published figures -- explicitly forbidden. `ensemble_arrivals` therefore
reads only `modelled_arrival_min` from each ensemble member, so a member dict
    carrying `public_timings_min`, `closest_member` or `mismatch_min` contributes none of
    them: they are ignored by construction rather than rejected. `read_sanity_key` is the
    one that raises, and it guards direct access to those keys."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from serac.errors import SeracError

FORBIDDEN_SANITY_KEYS: frozenset[str] = frozenset(
    {"public_timings_min", "closest_member", "mismatch_min", "public_timing_min"}
)
"""Keys in the M4 sanity artifact that carry, or are derived from, press-reported timings.

Nothing downstream of the ensemble may read these. They exist so a human can read the
mismatch; a model that consumed them would be selecting on the answer.
"""

ALLOWED_MEMBER_KEYS: frozenset[str] = frozenset(
    {"run_id", "modelled_arrival_min", "parameters", "transects_reached"}
)


class EvidenceError(SeracError):
    """An upstream artifact was missing, unreadable, or asked for something forbidden."""


class Execution(StrEnum):
    executed = "executed"
    artifact = "artifact"
    unavailable = "unavailable"


class StageOutcome(StrEnum):
    """What a stage did. `refused` is a first-class success of the refusal machinery."""

    produced = "produced"
    refused = "refused"
    did_not_fire = "did_not_fire"
    insufficient_input = "insufficient_input"
    not_reached = "not_reached"
    unavailable = "unavailable"


@dataclass(frozen=True)
class StageEvidence:
    """One stage of the chain, as run or as recorded."""

    stage: str
    component: str
    outcome: StageOutcome
    execution: Execution
    summary: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    artifact_generated_utc: str | None = None
    measured: dict[str, Any] = field(default_factory=dict)
    blocks_downstream: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "component": self.component,
            "outcome": self.outcome.value,
            "execution": self.execution.value,
            "summary": self.summary,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "artifact_generated_utc": self.artifact_generated_utc,
            "measured": self.measured,
            "blocks_downstream": self.blocks_downstream,
            "notes": list(self.notes),
        }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EvidenceError(f"{path}: artifact missing")
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


# -- M1 -----------------------------------------------------------------------------------------


def discriminator_latency(repo: Path, event_id: str) -> StageEvidence:
    """The recorded M1 latency run: did the detector fire, and how late in stream time?"""
    path = repo / "reports" / "m1" / f"latency_{event_id}.json"
    if not path.exists():
        return StageEvidence(
            stage="detection",
            component="M1 discriminator",
            outcome=StageOutcome.unavailable,
            execution=Execution.unavailable,
            summary=f"no recorded latency run at {path.relative_to(repo)}",
            blocks_downstream=True,
        )
    doc = _load(path)
    modes = doc.get("modes", [])
    fired = [m for m in modes if m.get("fired")]
    measured = {
        "n_receivers": doc.get("n_receivers"),
        "origin_utc": doc.get("origin_utc"),
        "modes": {
            m["mode"]: {
                "fired": m.get("fired"),
                "stream_latency_s": m.get("stream_latency_s"),
                "probability": m.get("probability"),
                "class_label": m.get("class_label"),
                "theoretical_floor_s": m.get("theoretical_floor_s"),
                "compute_seconds_per_scored_window": m.get("compute_seconds_per_scored_window"),
            }
            for m in modes
        },
        "budget_met": doc.get("budget_met"),
    }
    if not fired:
        return StageEvidence(
            stage="detection",
            component="M1 discriminator",
            outcome=StageOutcome.did_not_fire,
            execution=Execution.artifact,
            summary=(
                f"no mode fired: {', '.join(m['mode'] for m in modes)} all returned no candidate "
                f"on {doc.get('n_receivers')} receiver(s)"
            ),
            artifact_path=str(path.relative_to(repo)),
            artifact_sha256=sha256_of(path),
            artifact_generated_utc=doc.get("measured_at_utc"),
            measured=measured,
            blocks_downstream=True,
            notes=[str(doc.get("verdict", "")), *doc.get("notes", [])],
        )
    fastest = min(fired, key=lambda m: m.get("stream_latency_s") or math.inf)
    return StageEvidence(
        stage="detection",
        component="M1 discriminator",
        outcome=StageOutcome.produced,
        execution=Execution.artifact,
        summary=(
            f"fired in {len(fired)} of {len(modes)} mode(s); fastest {fastest['mode']} at "
            f"{fastest.get('stream_latency_s')} s after origin, calibrated p="
            f"{fastest.get('probability')}, class {fastest.get('class_label')}"
        ),
        artifact_path=str(path.relative_to(repo)),
        artifact_sha256=sha256_of(path),
        artifact_generated_utc=doc.get("measured_at_utc"),
        measured=measured,
        blocks_downstream=False,
        notes=[str(doc.get("verdict", "")), *doc.get("notes", [])],
    )


def discriminator_case_study(repo: Path, event_id: str) -> dict[str, Any] | None:
    """M1's single-window case study for an event, when one was written."""
    path = repo / "reports" / "m1" / f"case_study_{event_id}.json"
    return _load(path) if path.exists() else None


# -- M2 -----------------------------------------------------------------------------------------


def lfh_outcome(repo: Path, event_id: str, *, doc: dict[str, Any] | None = None) -> StageEvidence:
    """M2's force history for an event: produced, or refused with the geometry that refused it."""
    path = repo / "reports" / "m2" / f"{event_id}.json"
    if doc is None:
        if not path.exists():
            return StageEvidence(
                stage="lfh",
                component="M2 single-force inversion",
                outcome=StageOutcome.unavailable,
                execution=Execution.unavailable,
                summary=f"no inversion report at {path.relative_to(repo)}",
                blocks_downstream=True,
            )
        doc = _load(path)
        execution = Execution.artifact
    else:
        execution = Execution.executed
    force = doc.get("force_history", {})
    geometry = doc.get("geometry", {})
    status = force.get("status")
    measured = {
        "status": status,
        "azimuthal_gap_deg": force.get("azimuthal_gap_deg"),
        "variance_reduction": force.get("variance_reduction"),
        "n_stations": geometry.get("n_stations"),
        "n_channels": geometry.get("n_channels"),
        "median_pre_event_snr": geometry.get("median_pre_event_snr"),
        "stations": geometry.get("stations"),
        "mass": force.get("mass"),
        "wall_clock_s": doc.get("wall_clock_s"),
        "config_hash": doc.get("config_hash"),
    }
    if status == "computed":
        return StageEvidence(
            stage="lfh",
            component="M2 single-force inversion",
            outcome=StageOutcome.produced,
            execution=execution,
            summary="force history and mass estimate produced",
            artifact_path=str(path.relative_to(repo)) if path.exists() else None,
            artifact_sha256=sha256_of(path) if path.exists() else None,
            artifact_generated_utc=doc.get("generated_at_utc"),
            measured=measured,
        )
    return StageEvidence(
        stage="lfh",
        component="M2 single-force inversion",
        outcome=StageOutcome.refused,
        execution=execution,
        summary=str(force.get("notes") or f"status={status}"),
        artifact_path=str(path.relative_to(repo)) if path.exists() else None,
        artifact_sha256=sha256_of(path) if path.exists() else None,
        artifact_generated_utc=doc.get("generated_at_utc"),
        measured=measured,
        blocks_downstream=True,
        notes=[
            "M2 produces no mass, so the runout surrogate has no release volume to be given: "
            "the cascade forecast for this event cannot be built from serac's own chain."
        ],
    )


# -- M4 -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransectArrivalStats:
    """The frozen ensemble's arrival distribution at one transect. A prior, not a forecast."""

    transect_id: str
    members_total: int
    members_reaching: int
    p5_min: float | None
    p50_min: float | None
    p95_min: float | None

    @property
    def reach_fraction(self) -> float:
        return self.members_reaching / self.members_total if self.members_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "transect_id": self.transect_id,
            "members_total": self.members_total,
            "members_reaching": self.members_reaching,
            "reach_fraction": round(self.reach_fraction, 4),
            "p5_min": self.p5_min,
            "p50_min": self.p50_min,
            "p95_min": self.p95_min,
        }


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def ensemble_arrivals(repo: Path) -> tuple[list[TransectArrivalStats], StageEvidence]:
    """Per-transect arrival statistics over the frozen ensemble's own solver output.

    Reads only `all_members[].modelled_arrival_min`. Never the press timings, never the
    closest member, never a mismatch -- see `FORBIDDEN_SANITY_KEYS`.
    """
    path = repo / "reports" / "runout" / "langtang_sanity.json"
    if not path.exists():
        return [], StageEvidence(
            stage="runout",
            component="M4 runout ensemble",
            outcome=StageOutcome.unavailable,
            execution=Execution.unavailable,
            summary=f"no ensemble artifact at {path.relative_to(repo)}",
            blocks_downstream=True,
        )
    doc = _load(path)
    members = doc.get("all_members", [])
    per_transect: dict[str, list[float]] = {}
    for member in members:
        arrivals = _member_arrivals(member)
        for transect_id, minutes in arrivals.items():
            if minutes is not None:
                per_transect.setdefault(transect_id, []).append(float(minutes))
    known = sorted({t for m in members for t in _member_arrivals(m)})
    stats = [
        TransectArrivalStats(
            transect_id=transect_id,
            members_total=len(members),
            members_reaching=len(per_transect.get(transect_id, [])),
            p5_min=(
                _percentile(per_transect[transect_id], 0.05)
                if per_transect.get(transect_id)
                else None
            ),
            p50_min=(
                _percentile(per_transect[transect_id], 0.50)
                if per_transect.get(transect_id)
                else None
            ),
            p95_min=(
                _percentile(per_transect[transect_id], 0.95)
                if per_transect.get(transect_id)
                else None
            ),
        )
        for transect_id in known
    ]
    reached = [s for s in stats if s.members_reaching]
    return stats, StageEvidence(
        stage="runout",
        component="M4 runout ensemble (frozen design prior)",
        outcome=StageOutcome.produced if reached else StageOutcome.not_reached,
        execution=Execution.artifact,
        summary=(
            f"{len(members)} frozen members; {len(reached)} of {len(stats)} transect(s) reached "
            "by at least one member"
        ),
        artifact_path=str(path.relative_to(repo)),
        artifact_sha256=sha256_of(path),
        artifact_generated_utc=doc.get("generated_utc"),
        measured={
            "design_hash": doc.get("frozen_design_hash"),
            "solver_version": doc.get("frozen_solver_version"),
            "transects": [s.as_dict() for s in stats],
        },
        notes=[
            "This is the frozen ensemble's own arrival distribution over its Latin-hypercube "
            "design prior. It is a sampling design, NOT an estimate of the 26 August 2026 "
            "release, because M2 refused and no release volume for that event exists.",
            "Read from a whitelist of member keys; the press-comparison fields in this artifact "
            "are never read (serac.cascade.evidence.FORBIDDEN_SANITY_KEYS).",
        ],
    )


def _member_arrivals(member: dict[str, Any]) -> dict[str, float | None]:
    for forbidden in FORBIDDEN_SANITY_KEYS:
        if forbidden in ALLOWED_MEMBER_KEYS:  # pragma: no cover - guards the whitelist itself
            raise EvidenceError(f"{forbidden} must never be in ALLOWED_MEMBER_KEYS")
    arrivals = member.get("modelled_arrival_min")
    if not isinstance(arrivals, dict):
        return {}
    return {str(k): (None if v is None else float(v)) for k, v in arrivals.items()}


def read_sanity_key(repo: Path, key: str) -> Any:
    """Deliberate accessor for the M4 sanity artifact that refuses the press-derived keys."""
    if key in FORBIDDEN_SANITY_KEYS:
        raise EvidenceError(
            f"{key!r} carries or derives from press-reported timings; nothing downstream of the "
            "ensemble may read it (tuning toward published figures is forbidden)"
        )
    return _load(repo / "reports" / "runout" / "langtang_sanity.json").get(key)


def surrogate_metrics(repo: Path) -> dict[str, Any] | None:
    path = repo / "reports" / "runout" / "surrogate_metrics.json"
    return _load(path) if path.exists() else None


def surrogate_latency_s(repo: Path) -> float | None:
    """The surrogate's measured p95 inference latency, or None when it was never measured."""
    metrics = surrogate_metrics(repo)
    if not metrics:
        return None
    latency = metrics.get("latency", {})
    value = latency.get("p95_s")
    return float(value) if isinstance(value, int | float) else None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
