"""`validate-events`: the event library is complete, sourced, indexed and honest.

Checks (all `error` unless noted):

* every `data/events/*.json` parses and validates as a `MassMovementEvent`;
* `data/events/events.parquet` exists and is not stale;
* every `Range` cites at least one source that resolves;
* no `Range.best` without a source of a qualifying kind; press-only ranges carry `best: null`;
* exactly one `negative_control` (a `moraine_collapse_glof`), at least one
  `evacuation_counterfactual`, at least one `co_seismic_reference`, exactly one `target`
  whose `source_volume_m3` is null (the null-not-guess rule for the motivating event);
* every `sources[].sha256` appears in some ledger row (`data/manifest.jsonl`);
* every `transect_observations[].transect_id` exists in
  `data/aoi/<aoi_id>/transects.geojson` — a *warning* when the AOI directory or file is
  absent or the record has no `aoi_id`, an error when the file exists and the id is missing.

The pydantic model already enforces several of these; the suite re-asserts them on the files
as committed so that the report names every defect, not only the first.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.common import BEST_QUALIFYING_KINDS, SourceKind, iter_ranges
from serac.domain.events import EventRole, FailureType, MassMovementEvent
from serac.pipelines.events_index import INDEX_FILENAME, index_is_stale, record_paths
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "events"


def load_transect_ids(path: Path) -> set[str] | None:
    """`properties.id` of every feature in a transects GeoJSON, or None if unreadable."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    ids: set[str] = set()
    features = doc.get("features")
    if not isinstance(features, list):
        return None
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if isinstance(props, dict) and isinstance(props.get("id"), str):
            ids.add(props["id"])
    return ids


def _validation_summary(exc: ValidationError) -> str:
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        messages.append(f"{loc}: {err['msg']}")
    return "; ".join(messages)


def _check_records(suite: Suite, paths: list[Path]) -> list[MassMovementEvent]:
    records: list[MassMovementEvent] = []
    for path in paths:
        try:
            record = MassMovementEvent.model_validate_json(path.read_bytes())
        except ValidationError as exc:
            suite.check(f"record:{path.stem}", False, _validation_summary(exc))
            continue
        except ValueError as exc:
            suite.check(f"record:{path.stem}", False, f"{path.name}: {exc}")
            continue
        details = "" if path.stem == record.event_id else f"file name != event_id {record.event_id}"
        suite.check(f"record:{record.event_id}", not details, details)
        records.append(record)
    return records


def _check_ranges(suite: Suite, record: MassMovementEvent) -> None:
    by_id = {s.id: s for s in record.sources}
    unsourced: list[str] = []
    unqualified: list[str] = []
    press_best: list[str] = []
    for path, rng in iter_ranges(record):
        resolving = [r for r in rng.source_refs if r in by_id]
        if not resolving:
            unsourced.append(path)
            continue
        kinds = {by_id[r].kind for r in resolving}
        if rng.best is None:
            continue
        if not kinds & BEST_QUALIFYING_KINDS:
            unqualified.append(path)
        if kinds == {SourceKind.press_report}:
            press_best.append(path)
    eid = record.event_id
    suite.check(f"{eid}: every range sourced", not unsourced, ", ".join(unsourced))
    suite.check(
        f"{eid}: no best without qualifying source", not unqualified, ", ".join(unqualified)
    )
    suite.check(f"{eid}: press-only ranges carry best=null", not press_best, ", ".join(press_best))


def _check_ledger(suite: Suite, record: MassMovementEvent, ledger_sha256s: set[str]) -> None:
    missing = [s.id for s in record.sources if s.sha256 not in ledger_sha256s]
    suite.check(
        f"{record.event_id}: every source sha256 in ledger",
        not missing,
        "no ledger row carries the sha256 of: " + ", ".join(missing) if missing else "",
    )


def _check_transects(suite: Suite, repo: Path, record: MassMovementEvent) -> None:
    if not record.transect_observations:
        return
    eid = record.event_id
    wanted = sorted({t.transect_id for t in record.transect_observations})
    if record.aoi_id is None:
        suite.warn(
            f"{eid}: transects resolve in AOI",
            False,
            f"record has {len(wanted)} transect observation(s) but no aoi_id",
        )
        return
    path = repo / "data" / "aoi" / record.aoi_id / "transects.geojson"
    if not path.is_file():
        suite.warn(
            f"{eid}: transects resolve in AOI",
            False,
            f"{path.relative_to(repo)} is absent; cannot verify {', '.join(wanted)}",
        )
        return
    ids = load_transect_ids(path)
    if ids is None:
        suite.check(
            f"{eid}: transects resolve in AOI",
            False,
            f"{path.relative_to(repo)} is not a GeoJSON FeatureCollection",
        )
        return
    missing = [t for t in wanted if t not in ids]
    suite.check(
        f"{eid}: transects resolve in AOI",
        not missing,
        f"not in {path.relative_to(repo)}: " + ", ".join(missing) if missing else "",
    )


def _check_roles(suite: Suite, records: list[MassMovementEvent]) -> None:
    negatives = [r for r in records if r.role == EventRole.negative_control]
    suite.check(
        "roles: exactly one negative_control (moraine_collapse_glof)",
        len(negatives) == 1 and negatives[0].failure_type == FailureType.moraine_collapse_glof,
        f"found {[r.event_id for r in negatives]}",
    )
    counterfactuals = [r.event_id for r in records if r.role == EventRole.evacuation_counterfactual]
    suite.check(
        "roles: at least one evacuation_counterfactual",
        bool(counterfactuals),
        f"found {counterfactuals}",
    )
    co_seismic = [r.event_id for r in records if r.role == EventRole.co_seismic_reference]
    suite.check("roles: at least one co_seismic_reference", bool(co_seismic), f"found {co_seismic}")
    targets = [r for r in records if r.role == EventRole.target]
    suite.check(
        "roles: exactly one target", len(targets) == 1, f"found {[r.event_id for r in targets]}"
    )
    if len(targets) == 1:
        suite.check(
            "roles: target source_volume_m3 is null",
            targets[0].source_volume_m3 is None,
            f"{targets[0].event_id}: no peer-reviewed volume estimate exists; store null",
        )


def run_suite(repo: Path) -> SuiteResult:
    """Run every event-library check against `repo` and return the result."""
    suite = Suite(SUITE_NAME, repo)
    events_dir = repo / "data" / "events"
    paths = record_paths(events_dir)
    if not paths:
        suite.check("no records", False, f"{events_dir} holds no *.json event records")
        return suite.result()

    records = _check_records(suite, paths)

    index = events_dir / INDEX_FILENAME
    if suite.check(
        f"index: {INDEX_FILENAME} exists", index.is_file(), "run `serac events build-index`"
    ):
        suite.check(
            "index: up to date",
            not index_is_stale(events_dir),
            "records changed since the index was built; run `serac events build-index`",
        )

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    ledger_sha256s = {e.sha256 for e in ledger.entries() if e.sha256 is not None}

    for record in records:
        _check_ranges(suite, record)
        _check_ledger(suite, record, ledger_sha256s)
        _check_transects(suite, repo, record)

    _check_roles(suite, records)
    return suite.result()
