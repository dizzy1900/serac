"""The event-library record: `MassMovementEvent` and its parts.

Every numeric figure is a `Range` (or `Range | None`), every `Range` cites sources that are
present in `sources[]`, and every null figure is explained by a `FieldNote`. The aggregate
validator on `MassMovementEvent` is where those rules become mechanical rather than
conventional. Error messages name the offending field path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from serac.domain.common import (
    BEST_QUALIFYING_KINDS,
    DOMAIN_CONFIG,
    SEMVER_PATTERN,
    SINGLE_FORCE_QUALIFYING_KINDS,
    FieldNote,
    Range,
    RecordMeta,
    Slug,
    SourceRef,
    annotation_accepts,
    iter_field_paths,
    iter_models,
    iter_none_fields,
    iter_ranges,
    iter_source_ref_ids,
)

EVENT_CONTRACT_VERSION = "0.1.0"


class FailureType(StrEnum):
    """Initiation mechanism of the mass movement."""

    bedrock_rock_ice_avalanche = "bedrock_rock_ice_avalanche"
    glacier_detachment = "glacier_detachment"
    hanging_glacier_collapse = "hanging_glacier_collapse"
    moraine_collapse_glof = "moraine_collapse_glof"
    co_seismic_avalanche = "co_seismic_avalanche"
    unknown = "unknown"


class EventRole(StrEnum):
    """Why the event is in the library."""

    target = "target"
    reference = "reference"
    negative_control = "negative_control"
    evacuation_counterfactual = "evacuation_counterfactual"
    co_seismic_reference = "co_seismic_reference"


class EventTime(BaseModel):
    """Origin time with its uncertainty and the basis for it (e.g. a catalogue origin time)."""

    model_config = DOMAIN_CONFIG

    datetime_utc: AwareDatetime
    uncertainty_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    basis: str = Field(min_length=1, description="e.g. 'usgs_comcat_origin', 'seismic_onset'")
    source_refs: list[Slug] = Field(min_length=1)


class SourceLocation(BaseModel):
    """Where the failure initiated."""

    model_config = DOMAIN_CONFIG

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    uncertainty_radius_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    basis: str = Field(min_length=1, description="e.g. 'usgs_comcat_epicentre', 'mapped_scar'")
    source_refs: list[Slug] = Field(min_length=1)


class SeismicAttribution(BaseModel):
    """A catalogue entry or seismic study attributed to the event.

    `single_force=True` asserts that the signal has been interpreted as a landslide single
    force; the owning record must then cite a peer-reviewed or ComCat source for it.
    """

    model_config = DOMAIN_CONFIG

    usgs_id: str | None = Field(default=None, pattern=r"^[a-z0-9]+$")
    magnitude: Range | None = None
    mag_type: str | None = Field(default=None, min_length=1)
    agency_range: Range | None = Field(
        default=None, description="Spread of magnitudes reported across agencies"
    )
    single_force: bool = False
    source_refs: list[Slug] = Field(min_length=1)
    notes: str | None = None


class AssetType(StrEnum):
    hydropower_plant = "hydropower_plant"
    bridge = "bridge"
    settlement = "settlement"
    border_post = "border_post"
    road = "road"
    other = "other"


class ImpactKind(StrEnum):
    destroyed = "destroyed"
    damaged = "damaged"
    inundated = "inundated"
    evacuated = "evacuated"
    unaffected = "unaffected"
    unknown = "unknown"


class InfrastructureImpact(BaseModel):
    """What happened to one asset."""

    model_config = DOMAIN_CONFIG

    asset_name: str = Field(min_length=1)
    asset_type: AssetType
    asset_id: Slug | None = Field(default=None, description="`ExposedAsset.id` in the AOI")
    impact: ImpactKind
    description: str | None = None
    source_refs: list[Slug] = Field(min_length=1)


class Precursor(BaseModel):
    """An observation made before failure (monitoring anomaly, rockfall, evacuation order)."""

    model_config = DOMAIN_CONFIG

    kind: str = Field(min_length=1, description="e.g. 'displacement_acceleration'")
    observed_utc: AwareDatetime | None = None
    lead_time_days: Range | None = Field(
        default=None, description="Days between the observation/order and the failure"
    )
    description: str = Field(min_length=1)
    source_refs: list[Slug] = Field(min_length=1)


class TransectObservation(BaseModel):
    """What was observed at a river transect (arrival time, stage rise)."""

    model_config = DOMAIN_CONFIG

    transect_id: Slug
    arrival_time_min: Range | None = Field(
        default=None, description="Minutes after `time.datetime_utc`"
    )
    stage_rise_m: Range | None = None
    description: str | None = None
    source_refs: list[Slug] = Field(min_length=1)


class MassMovementEvent(BaseModel):
    """One event-library record (`data/events/<event_id>.json`)."""

    model_config = DOMAIN_CONFIG

    schema_version: str = Field(default=EVENT_CONTRACT_VERSION, pattern=SEMVER_PATTERN)
    event_id: Slug
    name: str = Field(min_length=1)
    event_group: Slug = Field(description="Groups multi-episode events (e.g. twin detachments)")
    role: EventRole
    aoi_id: Slug | None = None
    failure_type: FailureType
    time: EventTime
    source_location: SourceLocation

    source_elevation_m: Range | None = None
    fall_height_m: Range | None = None
    source_volume_m3: Range | None = None
    rock_fraction: Range | None = None
    bulked_volume_m3: Range | None = None
    runout_km: Range | None = None
    peak_velocity_ms: Range | None = None
    fatalities: Range | None = None

    seismic: SeismicAttribution | None = None
    related_seismic: list[SeismicAttribution] = Field(default_factory=list)
    dammed_river: bool | None = Field(
        default=None,
        description="None = not established in any retrieved source (needs a FieldNote)",
    )
    secondary_surge: bool | None = Field(
        default=None,
        description="None = not established in any retrieved source (needs a FieldNote)",
    )
    initially_reported_as: str | None = None

    infrastructure_impacts: list[InfrastructureImpact] = Field(default_factory=list)
    precursors_observed: list[Precursor] = Field(default_factory=list)
    transect_observations: list[TransectObservation] = Field(default_factory=list)

    field_notes: dict[str, FieldNote] = Field(
        default_factory=dict, description="Keyed by the dotted path of the null field"
    )
    sources: list[SourceRef] = Field(min_length=1)
    notes: str | None = None
    record: RecordMeta

    @classmethod
    def range_fields(cls) -> tuple[str, ...]:
        """Names of the top-level `Range | None` fields."""
        return tuple(
            name
            for name, info in cls.model_fields.items()
            if annotation_accepts(info.annotation, Range)
        )

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        problems.extend(self._check_sources())
        problems.extend(self._check_field_notes())
        problems.extend(self._check_role_coupling())
        problems.extend(self._check_seismic())
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _check_sources(self) -> list[str]:
        problems: list[str] = []
        ids = [s.id for s in self.sources]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"sources: duplicate source ids {duplicates}")
        by_id = {s.id: s for s in self.sources}
        for path, ref in iter_source_ref_ids(self):
            if ref not in by_id:
                problems.append(f"{path}: source {ref!r} is not in sources[]")
        field_paths = set(iter_field_paths(self))
        claimed: set[str] = set()
        for i, source in enumerate(self.sources):
            for claim in source.claims_supported:
                claimed.add(claim)
                if claim not in field_paths:
                    problems.append(
                        f"sources[{i}].claims_supported: {claim!r} is not a field of this record"
                    )
        for path, rng in iter_ranges(self):
            if path not in claimed:
                problems.append(f"{path}: no source lists this path in claims_supported")
            if rng.best is not None:
                kinds = {by_id[r].kind for r in rng.source_refs if r in by_id}
                if not kinds & BEST_QUALIFYING_KINDS:
                    problems.append(
                        f"{path}: best requires a source of kind "
                        f"{sorted(k.value for k in BEST_QUALIFYING_KINDS)}"
                    )
        return problems

    def _check_field_notes(self) -> list[str]:
        """Every null numeric must be explained, never silently absent.

        Non-indexed paths (top-level fields and `seismic.*`) need `field_notes[path]`.
        Indexed paths (a null `Range` inside a list item such as
        `precursors_observed[2].lead_time_days`) are explained either by
        `field_notes[path]` or by a non-empty `description`/`notes` on that list item,
        which keeps index bookkeeping out of `field_notes` (qa review, 2026-09-03).
        """
        problems: list[str] = []
        none_paths = dict(iter_none_fields(self))
        range_nulls = {p for p, ann in none_paths.items() if annotation_accepts(ann, Range)}
        needing = {p for p in range_nulls if "[" not in p}
        if self.seismic is None:
            needing.add("seismic")
        for flag in ("dammed_river", "secondary_surge"):
            if getattr(self, flag) is None:
                needing.add(flag)
        for path in sorted(needing - set(self.field_notes)):
            problems.append(f"{path}: is null but field_notes[{path!r}] is missing")
        items = {path: sub for path, sub in iter_models(self) if path.endswith("]")}
        for path in sorted(p for p in range_nulls if "[" in p and p not in self.field_notes):
            prefix = path.rsplit(".", 1)[0]
            item = items.get(prefix)
            explanation = None
            if item is not None:
                explanation = getattr(item, "description", None) or getattr(item, "notes", None)
            if not explanation:
                problems.append(
                    f"{path}: is null; give {prefix} a description/notes or field_notes[{path!r}]"
                )
        for key in sorted(set(self.field_notes) - set(none_paths)):
            problems.append(f"field_notes[{key!r}]: does not name a null field")
        return problems

    def _check_role_coupling(self) -> list[str]:
        problems: list[str] = []
        is_glof = self.failure_type == FailureType.moraine_collapse_glof
        if (self.role == EventRole.negative_control) != is_glof:
            problems.append(
                f"role={self.role.value} and failure_type={self.failure_type.value}: "
                "negative_control <=> moraine_collapse_glof"
            )
        is_co_seismic = self.failure_type == FailureType.co_seismic_avalanche
        if (self.role == EventRole.co_seismic_reference) != is_co_seismic:
            problems.append(
                f"role={self.role.value} and failure_type={self.failure_type.value}: "
                "co_seismic_reference <=> co_seismic_avalanche"
            )
        if is_co_seismic and (self.seismic is None or self.seismic.usgs_id is None):
            problems.append(
                "seismic.usgs_id: required for co_seismic_avalanche (the triggering quake)"
            )
        if self.role == EventRole.evacuation_counterfactual:
            if not any(p.lead_time_days is not None for p in self.precursors_observed):
                problems.append(
                    "precursors_observed: evacuation_counterfactual needs a precursor with "
                    "lead_time_days"
                )
            if not any(i.impact == ImpactKind.evacuated for i in self.infrastructure_impacts):
                problems.append(
                    "infrastructure_impacts: evacuation_counterfactual needs an "
                    "impact=evacuated entry"
                )
        return problems

    def _check_seismic(self) -> list[str]:
        problems: list[str] = []
        by_id = {s.id: s for s in self.sources}
        attributions: list[tuple[str, SeismicAttribution]] = []
        if self.seismic is not None:
            attributions.append(("seismic", self.seismic))
        attributions.extend(
            (f"related_seismic[{i}]", s) for i, s in enumerate(self.related_seismic)
        )
        for path, attribution in attributions:
            if attribution.single_force:
                kinds = {by_id[r].kind for r in attribution.source_refs if r in by_id}
                if not kinds & SINGLE_FORCE_QUALIFYING_KINDS:
                    problems.append(
                        f"{path}.single_force: requires a source of kind "
                        f"{sorted(k.value for k in SINGLE_FORCE_QUALIFYING_KINDS)}"
                    )
        usgs_ids = [a.usgs_id for _, a in attributions if a.usgs_id is not None]
        duplicates = sorted({u for u in usgs_ids if usgs_ids.count(u) > 1})
        if duplicates:
            problems.append(f"related_seismic: usgs_id repeated {duplicates}")
        return problems


CONTRACTS: dict[str, type[BaseModel]] = {"mass-movement-event": MassMovementEvent}
