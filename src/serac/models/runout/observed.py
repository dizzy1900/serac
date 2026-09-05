"""What the event record observed at each transect — the only source of a comparison target.

Why this module exists
----------------------
The Langtang comparison used to hold its targets as a literal in the source:

    PUBLIC_TIMINGS_MIN = {"rasuwagadhi-gyirong": 7.5, "syabrubesi": 13.5,
                          "betrawati": 45.0, "galchhi": 30.0}

Three of those four numbers were not observations the event library holds. `~7.5 min`
(Rasuwagadhi) and `~45 min` (Betrawati) circulate publicly with no retrievable source, and
`data/events/langtang-lhende-2026.json` records them as refused, with `arrival_time_min: null`
and a description saying so; `30 min` at Galchhi is the window of a **stage rise** (+9 m),
not an arrival time; and `13.5` was a midpoint of a "13-14 min" span the record does not carry
either — the record has 13 min, the difference of two clock times a source states. The write-up
nevertheless described all four as "press-attributed figures … the corresponding fields carry
`best: null`", which is a claim of provenance for figures that have none.

A literal in a module cannot be checked against anything, so the fix is not to correct the four
numbers: it is to remove the place where a number can be typed. `load_transect_targets` reads
`data/events/<event_id>.json` through `MassMovementEvent` and builds a comparison target **only**
where the record holds an `arrival_time_min` `Range`. Where the record holds `null`, there is no
target and the record's own sentence is carried through to the report as the reason. Every figure
the comparison quotes therefore arrives with the record's `source_refs` attached, and
`verify_targets_against_record` re-derives the targets from the record and fails
`validate-runout` if a committed artifact carries anything else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serac.domain.events import MassMovementEvent, TransectObservation
from serac.errors import SeracError

TARGET_EVENT_ID = "langtang-lhende-2026"
"""The record M4's comparison reads. The AOI's transects belong to this event."""

ARRIVAL_UNIT = "min"
"""The only unit an arrival target may carry. A different one is refused, never converted."""


class ObservedTimingError(SeracError):
    """The event record cannot supply the transect observations the comparison needs."""


def record_path(repo: Path, event_id: str = TARGET_EVENT_ID) -> Path:
    """Repo-relative location of the event record the comparison reads."""
    return repo / "data" / "events" / f"{event_id}.json"


@dataclass(frozen=True)
class TransectTarget:
    """One transect as the event record holds it.

    `arrival_low_min`/`arrival_high_min` are set only when the record carries an
    `arrival_time_min` `Range`; then, and only then, the transect is a comparison target.
    Otherwise `absent_reason` carries the record's own sentence about why there is no arrival
    time, and `other_observations` says what the record *does* hold there (a stage rise, say),
    so a reader cannot mistake one for the other.
    """

    transect_id: str
    arrival_low_min: float | None
    arrival_high_min: float | None
    arrival_best_min: float | None
    arrival_source_refs: tuple[str, ...]
    arrival_notes: str | None
    absent_reason: str | None
    other_observations: tuple[str, ...]

    @property
    def is_comparison_target(self) -> bool:
        """True only when the record holds an arrival time for this transect."""
        return self.arrival_low_min is not None and self.arrival_high_min is not None

    @property
    def label(self) -> str:
        """How the recorded arrival is written in a report, or `null` when there is none."""
        if not self.is_comparison_target:
            return "null"
        low, high = self.arrival_low_min, self.arrival_high_min
        assert low is not None and high is not None
        if low == high:
            return f"{low:g}"
        return f"{low:g}-{high:g}"

    def signed_gap_min(self, modelled_min: float) -> float:
        """Signed minutes from the recorded interval: negative early, positive late, 0 inside.

        Not a difference from a midpoint. A midpoint of a recorded interval is a number nobody
        published; the distance to the interval is a statement about the interval itself, and
        for a single-valued record (`low == high`) it reduces to the plain difference.
        """
        if not self.is_comparison_target:
            raise ObservedTimingError(
                f"{self.transect_id}: the event record holds no arrival time, so no mismatch "
                "against it can be computed"
            )
        low, high = self.arrival_low_min, self.arrival_high_min
        assert low is not None and high is not None
        if modelled_min < low:
            return modelled_min - low
        if modelled_min > high:
            return modelled_min - high
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        """The artifact form; `verify_targets_against_record` re-derives and compares this."""
        return {
            "transect_id": self.transect_id,
            "is_comparison_target": self.is_comparison_target,
            "arrival_low_min": self.arrival_low_min,
            "arrival_high_min": self.arrival_high_min,
            "arrival_best_min": self.arrival_best_min,
            "arrival_unit": ARRIVAL_UNIT if self.is_comparison_target else None,
            "arrival_source_refs": list(self.arrival_source_refs),
            "arrival_notes": self.arrival_notes,
            "absent_reason": self.absent_reason,
            "other_observations": list(self.other_observations),
        }


def _other_observations(observation: TransectObservation) -> tuple[str, ...]:
    """Everything numeric the record holds at this transect that is *not* an arrival time."""
    out: list[str] = []
    if observation.stage_rise_m is not None:
        rise = observation.stage_rise_m
        span = f"{rise.low:g}" if rise.low == rise.high else f"{rise.low:g}-{rise.high:g}"
        out.append(
            f"stage_rise_m {span} {rise.unit} "
            f"(sources: {', '.join(rise.source_refs)}; best "
            f"{'null' if rise.best is None else f'{rise.best:g}'})"
        )
    return tuple(out)


def _target_from_observation(observation: TransectObservation) -> TransectTarget:
    arrival = observation.arrival_time_min
    if arrival is None:
        if not (observation.description or "").strip():
            raise ObservedTimingError(
                f"{observation.transect_id}: arrival_time_min is null and the record gives no "
                "description saying why; a silent null cannot be reported as a reason"
            )
        return TransectTarget(
            transect_id=observation.transect_id,
            arrival_low_min=None,
            arrival_high_min=None,
            arrival_best_min=None,
            arrival_source_refs=(),
            arrival_notes=None,
            absent_reason=(observation.description or "").strip(),
            other_observations=_other_observations(observation),
        )
    if arrival.unit != ARRIVAL_UNIT:
        raise ObservedTimingError(
            f"{observation.transect_id}: arrival_time_min.unit is {arrival.unit!r}, not "
            f"{ARRIVAL_UNIT!r}; the comparison converts nothing"
        )
    return TransectTarget(
        transect_id=observation.transect_id,
        arrival_low_min=float(arrival.low),
        arrival_high_min=float(arrival.high),
        arrival_best_min=None if arrival.best is None else float(arrival.best),
        arrival_source_refs=tuple(arrival.source_refs),
        arrival_notes=arrival.notes,
        absent_reason=None,
        other_observations=_other_observations(observation),
    )


def targets_from_record(event: MassMovementEvent) -> tuple[TransectTarget, ...]:
    """Build one `TransectTarget` per transect observation, in the record's order."""
    seen: set[str] = set()
    out: list[TransectTarget] = []
    for observation in event.transect_observations:
        if observation.transect_id in seen:
            raise ObservedTimingError(
                f"{event.event_id}: transect {observation.transect_id!r} appears twice; the "
                "comparison cannot choose between two records of the same transect"
            )
        seen.add(observation.transect_id)
        out.append(_target_from_observation(observation))
    return tuple(out)


def load_transect_targets(
    repo: Path, event_id: str = TARGET_EVENT_ID
) -> tuple[TransectTarget, ...]:
    """Read the event record and derive its transect targets. Raises if the record is missing."""
    path = record_path(repo, event_id)
    if not path.exists():
        raise ObservedTimingError(
            f"{path} does not exist; the comparison has no observations to compare against and "
            "will not supply its own"
        )
    event = MassMovementEvent.model_validate_json(path.read_bytes())
    return targets_from_record(event)


def comparison_targets(targets: tuple[TransectTarget, ...]) -> dict[str, TransectTarget]:
    """The subset that carries a recorded arrival time, keyed by `transect_id`."""
    return {t.transect_id: t for t in targets if t.is_comparison_target}


def verify_targets_against_record(
    payload: dict[str, Any], repo: Path, event_id: str = TARGET_EVENT_ID
) -> list[str]:
    """Re-derive the targets from the record and list every disagreement with `payload`.

    An empty list means every comparison target in the artifact is a figure the event record
    holds, with the record's own bounds and sources, and that no transect the record leaves
    `null` is being compared against anything. `validate-runout` fails on any entry.
    """
    problems: list[str] = []
    try:
        expected = {t.transect_id: t for t in load_transect_targets(repo, event_id)}
    except (ObservedTimingError, ValueError) as exc:
        return [f"the event record could not be read: {exc}"]

    raw = payload.get("transect_targets")
    if not isinstance(raw, list):
        problems.append(
            "the artifact carries no 'transect_targets' block, so its comparison targets cannot "
            "be traced to the event record (regenerate with `serac runout langtang`)"
        )
        raw = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            problems.append(f"transect_targets entry is not an object: {entry!r}")
            continue
        transect_id = str(entry.get("transect_id"))
        seen.add(transect_id)
        want = expected.get(transect_id)
        if want is None:
            problems.append(
                f"{transect_id}: compared against a figure for a transect the record "
                f"{event_id} does not hold an observation for"
            )
            continue
        got = {k: entry.get(k) for k in ("arrival_low_min", "arrival_high_min", "arrival_unit")}
        want_dict = want.as_dict()
        wanted = {k: want_dict[k] for k in got}
        if got != wanted:
            problems.append(f"{transect_id}: artifact holds {got}, the record holds {wanted}")
        if bool(entry.get("is_comparison_target")) != want.is_comparison_target:
            problems.append(
                f"{transect_id}: artifact says is_comparison_target="
                f"{bool(entry.get('is_comparison_target'))}, the record implies "
                f"{want.is_comparison_target}"
            )
        if list(entry.get("arrival_source_refs") or []) != list(want.arrival_source_refs):
            problems.append(
                f"{transect_id}: artifact cites {entry.get('arrival_source_refs')!r}, the "
                f"record cites {list(want.arrival_source_refs)!r}"
            )
    for missing in sorted(set(expected) - seen):
        problems.append(
            f"{missing}: the record holds an observation for this transect and the artifact "
            "does not report it"
        )

    per_transect = payload.get("per_transect")
    if isinstance(per_transect, dict):
        for transect_id, block in per_transect.items():
            if not isinstance(block, dict):
                continue
            recorded = block.get("recorded_arrival_min")
            want = expected.get(str(transect_id))
            has_target = want is not None and want.is_comparison_target
            if recorded is not None and not has_target:
                problems.append(
                    f"{transect_id}: per_transect carries a recorded arrival {recorded!r} where "
                    "the event record holds none"
                )
            if recorded is None and has_target:
                problems.append(
                    f"{transect_id}: per_transect carries no recorded arrival where the event "
                    "record holds one"
                )
    return problems


def load_payload(path: Path) -> dict[str, Any]:
    """Read a JSON artifact, raising `ObservedTimingError` rather than returning half of one."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ObservedTimingError(f"{path} could not be read: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ObservedTimingError(f"{path} is not a JSON object")
    return loaded
