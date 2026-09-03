"""The committed event library: roles, ids and the null-not-guess rule, checked offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.common import BEST_QUALIFYING_KINDS, SourceKind, iter_ranges
from serac.domain.events import EventRole, FailureType, MassMovementEvent
from serac.pipelines.sources import dump_record

EXPECTED_IDS = {
    "kolka-karmadon-2002",
    "aru-co-2016-07",
    "aru-co-2016-09",
    "sedongpu-2017",
    "sedongpu-2018-10",
    "chamoli-2021",
    "marmolada-2022",
    "south-lhonak-2023",
    "blatten-2025",
    "langtang-2015",
    "langtang-lhende-2026",
}


@pytest.fixture(scope="module")
def events_dir(repo_root: Path) -> Path:
    return repo_root / "data" / "events"


@pytest.fixture(scope="module")
def records(events_dir: Path) -> dict[str, MassMovementEvent]:
    out: dict[str, MassMovementEvent] = {}
    for path in sorted(events_dir.glob("*.json")):
        record = MassMovementEvent.model_validate_json(path.read_text(encoding="utf-8"))
        assert record.event_id == path.stem, f"{path.name}: event_id does not match file name"
        out[record.event_id] = record
    return out


@pytest.fixture(scope="module")
def ledger_shas(repo_root: Path) -> set[str]:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    return {e.sha256 for e in ledger.entries() if e.sha256}


def test_all_eleven_records_present(records: dict[str, MassMovementEvent]) -> None:
    assert set(records) == EXPECTED_IDS


def test_canonical_json_form(events_dir: Path) -> None:
    for path in sorted(events_dir.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert text == dump_record(json.loads(text)), f"{path.name}: not sorted-keys/2-space form"


def test_roles_and_failure_types(records: dict[str, MassMovementEvent]) -> None:
    roles = {eid: r.role for eid, r in records.items()}
    assert roles["langtang-lhende-2026"] == EventRole.target
    assert roles["south-lhonak-2023"] == EventRole.negative_control
    assert records["south-lhonak-2023"].failure_type == FailureType.moraine_collapse_glof
    assert roles["blatten-2025"] == EventRole.evacuation_counterfactual
    assert roles["langtang-2015"] == EventRole.co_seismic_reference
    assert records["langtang-2015"].failure_type == FailureType.co_seismic_avalanche
    for eid in EXPECTED_IDS - {
        "langtang-lhende-2026",
        "south-lhonak-2023",
        "blatten-2025",
        "langtang-2015",
    }:
        assert roles[eid] == EventRole.reference, eid
    assert sum(1 for r in roles.values() if r == EventRole.target) == 1
    assert sum(1 for r in roles.values() if r == EventRole.negative_control) == 1


def test_event_groups(records: dict[str, MassMovementEvent]) -> None:
    assert records["aru-co-2016-07"].event_group == "aru-co-2016"
    assert records["aru-co-2016-09"].event_group == "aru-co-2016"
    assert records["sedongpu-2017"].event_group == "sedongpu-2017-2018"
    assert records["sedongpu-2018-10"].event_group == "sedongpu-2017-2018"


def test_aoi_ids(records: dict[str, MassMovementEvent]) -> None:
    assert records["langtang-lhende-2026"].aoi_id == "lhende-khola-trishuli"
    assert records["chamoli-2021"].aoi_id == "chamoli-rishiganga"
    assert records["blatten-2025"].aoi_id == "blatten-lotschental"
    for eid in EXPECTED_IDS - {"langtang-lhende-2026", "chamoli-2021", "blatten-2025"}:
        assert records[eid].aoi_id is None, eid


def test_target_event_is_honest(records: dict[str, MassMovementEvent]) -> None:
    target = records["langtang-lhende-2026"]
    assert target.source_volume_m3 is None
    note = target.field_notes["source_volume_m3"]
    assert note.public_estimates, "public estimates must be attributed in the FieldNote"
    assert all(e.source_ref in {s.id for s in target.sources} for e in note.public_estimates)
    assert target.fall_height_m is None
    assert "fall_height_m" in target.field_notes
    assert target.seismic is not None and target.seismic.usgs_id == "us7000tbwb"
    assert [s.usgs_id for s in target.related_seismic] == ["us7000tc90"]
    assert target.initially_reported_as is not None
    assert {t.transect_id for t in target.transect_observations} == {
        "rasuwagadhi-gyirong",
        "syabrubesi",
        "betrawati",
        "galchhi",
    }
    # Press-only transect figures never carry a best.
    for obs in target.transect_observations:
        for rng in (obs.arrival_time_min, obs.stage_rise_m):
            if rng is not None:
                assert rng.best is None


def test_co_seismic_reference_uses_gorkha(records: dict[str, MassMovementEvent]) -> None:
    seismic = records["langtang-2015"].seismic
    assert seismic is not None
    assert seismic.usgs_id == "us20002926"
    assert any(s.kind == SourceKind.usgs_comcat for s in records["langtang-2015"].sources)


def test_evacuation_counterfactual_has_official_lead_time(
    records: dict[str, MassMovementEvent],
) -> None:
    blatten = records["blatten-2025"]
    by_id = {s.id: s for s in blatten.sources}
    lead = [p for p in blatten.precursors_observed if p.lead_time_days is not None]
    assert lead, "an evacuation precursor with a lead time is required"
    kinds = {by_id[r].kind for p in lead for r in p.lead_time_days.source_refs}  # type: ignore[union-attr]
    assert SourceKind.agency_official in kinds


def test_sedongpu_volume_disagreement_is_explicit(records: dict[str, MassMovementEvent]) -> None:
    rng = records["sedongpu-2018-10"].source_volume_m3
    assert rng is not None and rng.disputed and rng.best is None
    assert len({e.source_ref for e in rng.estimates}) >= 2


def test_every_source_sha256_is_in_the_ledger(
    records: dict[str, MassMovementEvent], ledger_shas: set[str]
) -> None:
    for record in records.values():
        for source in record.sources:
            assert source.sha256 in ledger_shas, f"{record.event_id}/{source.id}: not ledgered"


def test_no_best_on_press_only_ranges(records: dict[str, MassMovementEvent]) -> None:
    for record in records.values():
        by_id = {s.id: s for s in record.sources}
        for path, rng in iter_ranges(record):
            if rng.best is None:
                continue
            kinds = {by_id[r].kind for r in rng.source_refs}
            assert kinds & BEST_QUALIFYING_KINDS, (
                f"{record.event_id}: {path} has best without a qualifying source"
            )


def test_press_sources_only_on_2025_2026_events(records: dict[str, MassMovementEvent]) -> None:
    for record in records.values():
        if any(s.kind == SourceKind.press_report for s in record.sources):
            assert record.time.datetime_utc.year >= 2025, record.event_id


def test_sources_are_never_wikipedia_or_social(records: dict[str, MassMovementEvent]) -> None:
    banned = ("wikipedia.org", "twitter.com", "x.com/", "facebook.com", "reddit.com", "medium.com")
    for record in records.values():
        for source in record.sources:
            assert not any(b in source.url for b in banned), f"{record.event_id}/{source.id}"
            assert source.accessed_utc.year == 2026


def test_dois_only_with_publisher_or_crossref_metadata(
    records: dict[str, MassMovementEvent],
) -> None:
    for record in records.values():
        for source in record.sources:
            if source.doi is not None:
                assert source.authors or source.publisher, (
                    f"{source.id}: DOI without resolved metadata"
                )
