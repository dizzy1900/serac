"""The interactive prompt sequence behind `serac events add`.

`interactive_record(ask)` walks an operator through every field of a `MassMovementEvent` and
returns the plain dict the CLI then validates. It is written against a single injectable
`ask(question) -> str` so tests drive it with a scripted list of answers and the CLI wires it
to `typer.prompt`. Nothing here validates beyond re-prompting for empty required answers,
unparsable numbers and out-of-vocabulary choices: the model is the judge, and an invalid
record is rejected before anything is written.

The list fields (`infrastructure_impacts`, `precursors_observed`, `transect_observations`,
`related_seismic`) are not prompted for; enter them with `serac events add --from-json`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from serac.domain.common import FieldNoteReason, SourceKind
from serac.domain.events import EventRole, FailureType, MassMovementEvent

Asker = Callable[[str], str]

UNKNOWN = "unknown"
MIN_NOTE_CHARS = 20
RANGE_HINT = f"(low,high[,best] or '{UNKNOWN}')"


class Prompter:
    """Typed questions on top of a string-in/string-out `ask` callable."""

    def __init__(self, ask: Asker) -> None:
        self.ask = ask

    def text(self, question: str) -> str:
        """A required free-text answer; re-asks until non-empty."""
        while True:
            answer = self.ask(question).strip()
            if answer:
                return answer

    def optional(self, question: str) -> str | None:
        """Free text; blank means null."""
        answer = self.ask(f"{question} [blank = null]").strip()
        return answer or None

    def choice(self, question: str, choices: Sequence[str]) -> str:
        options = "/".join(choices)
        while True:
            answer = self.ask(f"{question} ({options})").strip()
            if answer in choices:
                return answer

    def confirm(self, question: str) -> bool:
        answer = self.ask(f"{question} [y/N]").strip().lower()
        return answer in ("y", "yes")

    def csv(self, question: str, *, required: bool) -> list[str]:
        while True:
            raw = self.ask(f"{question} (comma-separated)")
            items = [part.strip() for part in raw.split(",") if part.strip()]
            if items or not required:
                return items

    def number(self, question: str) -> float:
        while True:
            try:
                return float(self.ask(question).strip())
            except ValueError:
                continue

    def optional_number(self, question: str) -> float | None:
        while True:
            raw = self.ask(f"{question} [blank = null]").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                continue

    def optional_int(self, question: str) -> int | None:
        while True:
            raw = self.ask(f"{question} [blank = null]").strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                continue

    def long_text(self, question: str, min_chars: int) -> str:
        while True:
            answer = self.ask(f"{question} (at least {min_chars} characters)").strip()
            if len(answer) >= min_chars:
                return answer


def _values(cls: type[Any]) -> list[str]:
    return [member.value for member in cls]


def prompt_field_note(p: Prompter, path: str) -> dict[str, Any]:
    """A `FieldNote` explaining why `path` is null."""
    reason = p.choice(f"{path}: reason it is null", _values(FieldNoteReason))
    notes = p.long_text(f"{path}: notes (list public estimates, each attributed)", MIN_NOTE_CHARS)
    return {"reason": reason, "public_estimates": [], "notes": notes}


def _parse_range_values(raw: str) -> list[float] | None:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) not in (2, 3):
        return None
    try:
        return [float(part) for part in parts]
    except ValueError:
        return None


def prompt_range(p: Prompter, path: str, field_notes: dict[str, Any]) -> dict[str, Any] | None:
    """A `Range` dict for `path`, or None after recording a `FieldNote` for it."""
    while True:
        raw = p.ask(f"{path} {RANGE_HINT}").strip()
        if raw.lower() == UNKNOWN:
            field_notes[path] = prompt_field_note(p, path)
            return None
        values = _parse_range_values(raw)
        if values is not None:
            break
    unit = p.text(f"{path}: unit")
    source_refs = p.csv(f"{path}: source ids", required=True)
    rng: dict[str, Any] = {
        "low": values[0],
        "high": values[1],
        "best": values[2] if len(values) == 3 else None,
        "unit": unit,
        "source_refs": source_refs,
    }
    return rng


def prompt_source(p: Prompter) -> dict[str, Any]:
    """Every `SourceRef` field; `peer_reviewed` follows from `kind`."""
    kind = p.choice("source kind", _values(SourceKind))
    return {
        "id": p.text("source id (slug)"),
        "kind": kind,
        "title": p.text("title"),
        "url": p.text("url"),
        "doi": p.optional("doi"),
        "authors": p.csv("authors", required=False),
        "year": p.optional_int("year"),
        "publisher": p.optional("publisher"),
        "accessed_utc": p.text("accessed_utc (ISO 8601, timezone-aware)"),
        "sha256": p.text("sha256 of the retrieved bytes"),
        "content_type": p.text("content_type"),
        "licence": p.text("licence"),
        "stored_copy": p.optional("stored_copy (repo path)"),
        "claims_supported": p.csv("claims_supported (field paths)", required=True),
        "excerpt": p.optional("excerpt"),
        "peer_reviewed": kind == SourceKind.peer_reviewed.value,
    }


def prompt_seismic(p: Prompter, field_notes: dict[str, Any]) -> dict[str, Any] | None:
    """The `seismic` attribution, or None (with `field_notes['seismic']` recorded)."""
    if not p.confirm("seismic attribution?"):
        field_notes["seismic"] = prompt_field_note(p, "seismic")
        return None
    usgs_id = p.optional("seismic.usgs_id")
    magnitude = prompt_range(p, "seismic.magnitude", field_notes)
    mag_type = p.optional("seismic.mag_type")
    agency_range = prompt_range(p, "seismic.agency_range", field_notes)
    source_refs = p.csv("seismic.source_refs", required=True)
    return {
        "usgs_id": usgs_id,
        "magnitude": magnitude,
        "mag_type": mag_type,
        "agency_range": agency_range,
        "single_force": p.confirm(
            "seismic.single_force (interpreted as a landslide single force)?"
        ),
        "source_refs": source_refs,
        "notes": p.optional("seismic.notes"),
    }


def interactive_record(ask: Asker, now: datetime | None = None) -> dict[str, Any]:
    """Walk the whole prompt sequence and return the record as a plain dict."""
    p = Prompter(ask)
    field_notes: dict[str, Any] = {}
    record: dict[str, Any] = {
        "event_id": p.text("event_id (slug)"),
        "name": p.text("name"),
        "event_group": p.text("event_group (slug)"),
        "role": p.choice("role", _values(EventRole)),
        "aoi_id": p.optional("aoi_id"),
        "failure_type": p.choice("failure_type", _values(FailureType)),
    }
    record["time"] = {
        "datetime_utc": p.text("time.datetime_utc (ISO 8601, timezone-aware)"),
        "uncertainty_s": p.optional_number("time.uncertainty_s"),
        "basis": p.text("time.basis"),
        "source_refs": p.csv("time.source_refs", required=True),
    }
    record["source_location"] = {
        "lat": p.number("source_location.lat"),
        "lon": p.number("source_location.lon"),
        "uncertainty_radius_m": p.optional_number("source_location.uncertainty_radius_m"),
        "basis": p.text("source_location.basis"),
        "source_refs": p.csv("source_location.source_refs", required=True),
    }
    for name in MassMovementEvent.range_fields():
        record[name] = prompt_range(p, name, field_notes)
    record["dammed_river"] = p.confirm("dammed_river?")
    record["secondary_surge"] = p.confirm("secondary_surge?")
    record["initially_reported_as"] = p.optional("initially_reported_as")
    record["notes"] = p.optional("notes")
    record["seismic"] = prompt_seismic(p, field_notes)
    record["related_seismic"] = []
    record["infrastructure_impacts"] = []
    record["precursors_observed"] = []
    record["transect_observations"] = []
    sources: list[dict[str, Any]] = []
    while p.confirm("add a source?"):
        sources.append(prompt_source(p))
    record["sources"] = sources
    record["field_notes"] = field_notes
    record["record"] = {
        "created_utc": (now or datetime.now(tz=UTC)).isoformat(),
        "created_by": p.text("record.created_by"),
    }
    return record
