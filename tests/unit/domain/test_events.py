from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from serac.domain import events
from serac.domain.common import (
    AttributedEstimate,
    FieldNote,
    FieldNoteReason,
    Range,
    SourceKind,
    SourceRef,
    iter_ranges,
)
from serac.domain.events import (
    AssetType,
    EventRole,
    FailureType,
    ImpactKind,
    InfrastructureImpact,
    MassMovementEvent,
    Precursor,
    SeismicAttribution,
    TransectObservation,
)

EventFactory = Callable[..., MassMovementEvent]
SourceFactory = Callable[..., SourceRef]
RangeFactory = Callable[..., Range]


def test_minimal_fictional_record_is_valid(make_event: EventFactory) -> None:
    ev = make_event()
    assert ev.schema_version == events.EVENT_CONTRACT_VERSION
    assert [p for p, _ in iter_ranges(ev)] == ["fall_height_m"]
    again = MassMovementEvent.model_validate_json(ev.model_dump_json())
    assert again == ev


def test_range_fields_lists_every_top_level_range() -> None:
    assert MassMovementEvent.range_fields() == (
        "source_elevation_m",
        "fall_height_m",
        "source_volume_m3",
        "rock_fraction",
        "bulked_volume_m3",
        "runout_km",
        "peak_velocity_ms",
        "fatalities",
    )


def test_failure_type_values_match_brief() -> None:
    assert {f.value for f in FailureType} == {
        "bedrock_rock_ice_avalanche",
        "glacier_detachment",
        "hanging_glacier_collapse",
        "moraine_collapse_glof",
        "co_seismic_avalanche",
        "unknown",
    }


# --- source resolution ----------------------------------------------------------------------


def test_unresolved_source_ref_names_the_path(
    make_event: EventFactory, make_range: RangeFactory
) -> None:
    with pytest.raises(
        ValidationError, match=r"fall_height_m.source_refs\[0\]: source 'test-nope'"
    ):
        make_event(fall_height_m=make_range(source_refs=["test-nope"]))


def test_unresolved_source_in_field_note_estimate(make_event: EventFactory) -> None:
    note = FieldNote(
        reason=FieldNoteReason.no_peer_reviewed_estimate,
        public_estimates=[
            AttributedEstimate(low=1.0, high=2.0, unit="m3", source_ref="test-ghost")
        ],
        notes="fictional: attributed public estimates only",
    )
    with pytest.raises(
        ValidationError,
        match=r"field_notes.source_volume_m3.public_estimates\[0\].source_ref: source 'test-ghost'",
    ):
        make_event(field_notes={"source_volume_m3": note})


def test_duplicate_source_ids(
    make_event: EventFactory, make_source: SourceFactory, event_kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match=r"sources: duplicate source ids \['test-src-1'\]"):
        make_event(sources=[*event_kwargs["sources"], make_source()])


def test_claim_must_name_a_field(make_event: EventFactory, make_source: SourceFactory) -> None:
    with pytest.raises(
        ValidationError,
        match=r"sources\[0\].claims_supported: 'fall_height_mm' is not a field of this record",
    ):
        make_event(sources=[make_source(claims=["fall_height_m", "fall_height_mm"])])


def test_every_range_path_must_be_claimed(
    make_event: EventFactory, make_source: SourceFactory
) -> None:
    with pytest.raises(
        ValidationError, match="fall_height_m: no source lists this path in claims_supported"
    ):
        make_event(sources=[make_source(claims=["time"])])


def test_best_needs_a_qualifying_source_kind(
    make_event: EventFactory, make_source: SourceFactory, make_range: RangeFactory
) -> None:
    claims = ["fall_height_m", "time", "source_location", "seismic"]
    press = make_source(kind=SourceKind.press_report, claims=claims)
    with pytest.raises(ValidationError, match="fall_height_m: best requires a source of kind"):
        make_event(sources=[press])
    assert make_event(sources=[press], fall_height_m=make_range(best=None)).fall_height_m
    for kind in (SourceKind.usgs_comcat, SourceKind.agency_official, SourceKind.dataset):
        assert make_event(sources=[make_source(kind=kind, claims=claims)]).fall_height_m


def test_best_qualifies_via_any_of_several_refs(
    make_event: EventFactory, make_source: SourceFactory, make_range: RangeFactory
) -> None:
    claims = ["fall_height_m", "time", "source_location", "seismic"]
    ev = make_event(
        sources=[
            make_source(kind=SourceKind.press_report, claims=claims),
            make_source(id="test-src-2", claims=["fall_height_m"]),
        ],
        fall_height_m=make_range(best=1.5, source_refs=["test-src-1", "test-src-2"]),
    )
    assert ev.fall_height_m is not None and ev.fall_height_m.best == 1.5


# --- null-needs-note ------------------------------------------------------------------------


def test_null_range_without_note_is_rejected(
    make_event: EventFactory, event_kwargs: dict[str, Any]
) -> None:
    notes = dict(event_kwargs["field_notes"])
    del notes["source_volume_m3"]
    with pytest.raises(
        ValidationError, match=r"source_volume_m3: is null but field_notes\['source_volume_m3'\]"
    ):
        make_event(field_notes=notes)


def test_nested_null_range_needs_note(
    make_event: EventFactory, event_kwargs: dict[str, Any]
) -> None:
    notes = dict(event_kwargs["field_notes"])
    del notes["seismic.agency_range"]
    with pytest.raises(ValidationError, match=r"seismic.agency_range: is null but"):
        make_event(field_notes=notes)


def test_list_item_null_range_needs_note(
    make_event: EventFactory, event_kwargs: dict[str, Any], field_note: FieldNote
) -> None:
    bare = TransectObservation(transect_id="test-t", source_refs=["test-src-1"])
    with pytest.raises(
        ValidationError,
        match=r"transect_observations\[0\].arrival_time_min: is null; give",
    ):
        make_event(transect_observations=[bare])
    explained = TransectObservation(
        transect_id="test-t", source_refs=["test-src-1"], description="no timing published"
    )
    assert make_event(transect_observations=[explained]).transect_observations
    notes = {
        **event_kwargs["field_notes"],
        "transect_observations[0].arrival_time_min": field_note,
        "transect_observations[0].stage_rise_m": field_note,
    }
    assert make_event(transect_observations=[bare], field_notes=notes).transect_observations


def test_orphan_field_note_is_rejected(
    make_event: EventFactory, event_kwargs: dict[str, Any], field_note: FieldNote
) -> None:
    with pytest.raises(
        ValidationError, match=r"field_notes\['fall_height_m'\]: does not name a null"
    ):
        make_event(field_notes={**event_kwargs["field_notes"], "fall_height_m": field_note})


def test_note_on_any_null_field_is_allowed(
    make_event: EventFactory, event_kwargs: dict[str, Any], field_note: FieldNote
) -> None:
    ev = make_event(field_notes={**event_kwargs["field_notes"], "aoi_id": field_note})
    assert "aoi_id" in ev.field_notes


def test_null_seismic_needs_note(
    make_event: EventFactory, event_kwargs: dict[str, Any], field_note: FieldNote
) -> None:
    notes = {k: v for k, v in event_kwargs["field_notes"].items() if not k.startswith("seismic.")}
    with pytest.raises(ValidationError, match=r"seismic: is null but field_notes\['seismic'\]"):
        make_event(seismic=None, field_notes=notes)
    ev = make_event(seismic=None, field_notes={**notes, "seismic": field_note})
    assert ev.seismic is None


# --- role / failure-type coupling -----------------------------------------------------------


def test_negative_control_requires_glof(make_event: EventFactory) -> None:
    with pytest.raises(ValidationError, match="negative_control <=> moraine_collapse_glof"):
        make_event(role=EventRole.negative_control)
    with pytest.raises(ValidationError, match="negative_control <=> moraine_collapse_glof"):
        make_event(failure_type=FailureType.moraine_collapse_glof)
    ev = make_event(role=EventRole.negative_control, failure_type=FailureType.moraine_collapse_glof)
    assert ev.role is EventRole.negative_control


def test_co_seismic_coupling_and_usgs_id(make_event: EventFactory) -> None:
    with pytest.raises(ValidationError, match="co_seismic_reference <=> co_seismic_avalanche"):
        make_event(role=EventRole.co_seismic_reference)
    with pytest.raises(ValidationError, match="co_seismic_reference <=> co_seismic_avalanche"):
        make_event(failure_type=FailureType.co_seismic_avalanche)
    with pytest.raises(
        ValidationError, match=r"seismic\.usgs_id: required for co_seismic_avalanche"
    ):
        make_event(
            role=EventRole.co_seismic_reference,
            failure_type=FailureType.co_seismic_avalanche,
            seismic=SeismicAttribution(source_refs=["test-src-1"]),
        )
    ev = make_event(
        role=EventRole.co_seismic_reference, failure_type=FailureType.co_seismic_avalanche
    )
    assert ev.seismic is not None and ev.seismic.usgs_id == "testid1"


def _precursor(make_range: RangeFactory, with_lead: bool) -> Precursor:
    return Precursor(
        kind="evacuation_order",
        lead_time_days=make_range(unit="days") if with_lead else None,
        description="fictional order",
        source_refs=["test-src-1"],
    )


def _evacuated() -> InfrastructureImpact:
    return InfrastructureImpact(
        asset_name="Fictional village",
        asset_type=AssetType.settlement,
        impact=ImpactKind.evacuated,
        source_refs=["test-src-1"],
    )


def test_evacuation_counterfactual_rules(
    make_event: EventFactory,
    make_range: RangeFactory,
    make_source: SourceFactory,
    event_kwargs: dict[str, Any],
    field_note: FieldNote,
) -> None:
    claims = [
        "fall_height_m",
        "time",
        "source_location",
        "seismic",
        "precursors_observed[0].lead_time_days",
    ]
    src = make_source(claims=claims)
    notes = {**event_kwargs["field_notes"], "precursors_observed[0].lead_time_days": field_note}
    with pytest.raises(ValidationError, match="needs a precursor with lead_time_days"):
        make_event(
            role=EventRole.evacuation_counterfactual,
            precursors_observed=[_precursor(make_range, with_lead=False)],
            infrastructure_impacts=[_evacuated()],
            field_notes=notes,
        )
    with pytest.raises(ValidationError, match="needs an impact=evacuated entry"):
        make_event(
            role=EventRole.evacuation_counterfactual,
            precursors_observed=[_precursor(make_range, with_lead=True)],
            sources=[src],
        )
    ev = make_event(
        role=EventRole.evacuation_counterfactual,
        precursors_observed=[_precursor(make_range, with_lead=True)],
        infrastructure_impacts=[_evacuated()],
        sources=[src],
    )
    assert ev.precursors_observed[0].lead_time_days is not None


# --- seismic --------------------------------------------------------------------------------


def test_single_force_needs_peer_reviewed_or_comcat(
    make_event: EventFactory, make_source: SourceFactory
) -> None:
    claims = ["fall_height_m", "time", "source_location", "seismic"]
    agency = make_source(kind=SourceKind.agency_official, claims=claims)
    attribution = SeismicAttribution(single_force=True, source_refs=["test-src-1"])
    with pytest.raises(ValidationError, match=r"seismic\.single_force: requires a source of kind"):
        make_event(seismic=attribution, sources=[agency])
    ok = make_event(
        seismic=attribution, sources=[make_source(kind=SourceKind.usgs_comcat, claims=claims)]
    )
    assert ok.seismic is not None and ok.seismic.single_force


def test_related_seismic_single_force_and_duplicate_ids(
    make_event: EventFactory,
    make_source: SourceFactory,
    event_kwargs: dict[str, Any],
    field_note: FieldNote,
) -> None:
    notes = {
        **event_kwargs["field_notes"],
        "related_seismic[0].magnitude": field_note,
        "related_seismic[0].agency_range": field_note,
    }
    claims = ["fall_height_m", "time", "source_location", "seismic"]
    agency = make_source(kind=SourceKind.agency_official, claims=claims)
    with pytest.raises(ValidationError, match=r"related_seismic\[0\].single_force: requires"):
        make_event(
            related_seismic=[SeismicAttribution(single_force=True, source_refs=["test-src-1"])],
            sources=[agency],
            field_notes=notes,
        )
    with pytest.raises(ValidationError, match=r"related_seismic: usgs_id repeated \['testid1'\]"):
        make_event(
            related_seismic=[SeismicAttribution(usgs_id="testid1", source_refs=["test-src-1"])],
            field_notes=notes,
        )
    ev = make_event(
        related_seismic=[SeismicAttribution(usgs_id="testid2", source_refs=["test-src-1"])],
        field_notes=notes,
    )
    assert ev.related_seismic[0].usgs_id == "testid2"


def test_seismic_attribution_constraints() -> None:
    with pytest.raises(ValidationError, match="usgs_id"):
        SeismicAttribution(usgs_id="US-7000", source_refs=["test-src-1"])
    with pytest.raises(ValidationError, match="source_refs"):
        SeismicAttribution(source_refs=[])


def test_multiple_problems_reported_together(
    make_event: EventFactory, make_source: SourceFactory
) -> None:
    with pytest.raises(ValidationError) as exc:
        make_event(role=EventRole.negative_control, sources=[make_source(claims=["time"])])
    text = str(exc.value)
    assert "negative_control <=> moraine_collapse_glof" in text
    assert "fall_height_m: no source lists this path" in text


def test_extra_fields_forbidden(make_event: EventFactory) -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_event(volume_guess=42)


def test_contracts_table() -> None:
    assert {"mass-movement-event": MassMovementEvent} == events.CONTRACTS


def test_unknown_dam_or_surge_flag_needs_a_note(
    make_event: EventFactory, event_kwargs: dict[str, Any], field_note: FieldNote
) -> None:
    with pytest.raises(ValidationError, match=r"dammed_river: is null but"):
        make_event(dammed_river=None)
    ev = make_event(
        dammed_river=None,
        secondary_surge=None,
        field_notes={
            **event_kwargs["field_notes"],
            "dammed_river": field_note,
            "secondary_surge": field_note,
        },
    )
    assert ev.dammed_river is None and ev.secondary_surge is None
