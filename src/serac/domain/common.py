"""Shared value objects for every serac domain contract.

The governing failure mode of this domain is fabricated precision. These types make the
"unknowns are null, not guesses" rule enforceable by the models themselves:

* every numeric figure is a `Range` that must cite at least one `SourceRef`;
* a `Range` may carry a single `best` value only when the record can show a qualifying source
  (checked by the aggregate that owns the `sources[]` list, e.g. `MassMovementEvent`);
* a `Range` that is `disputed` cannot carry a `best` at all and must attribute each estimate;
* a nullable numeric field that is `None` must be explained by a `FieldNote`.

`serac.domain` imports only the standard library and pydantic.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Self, Union, get_args, get_origin

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DOMAIN_CONFIG = ConfigDict(extra="forbid", frozen=True)

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
DOI_PATTERN = r"^10\.\d{4,9}/\S+$"
URL_PATTERN = r"^https?://\S+$"
FIELD_PATH_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\]|\.[A-Za-z0-9_-]+)*$"
SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"

Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
FieldPath = Annotated[str, Field(pattern=FIELD_PATH_PATTERN)]


class SourceKind(StrEnum):
    """What kind of document a `SourceRef` points at."""

    peer_reviewed = "peer_reviewed"
    preprint = "preprint"
    conference_abstract = "conference_abstract"
    usgs_comcat = "usgs_comcat"
    agency_official = "agency_official"
    dataset = "dataset"
    press_report = "press_report"
    operator_statement = "operator_statement"  # an operator/owner page about its own asset


BEST_QUALIFYING_KINDS: frozenset[SourceKind] = frozenset(
    {
        SourceKind.peer_reviewed,
        SourceKind.usgs_comcat,
        SourceKind.agency_official,
        SourceKind.dataset,
    }
)
"""Source kinds that allow a `Range` to carry a `best` value. Press-only ranges cannot."""

SINGLE_FORCE_QUALIFYING_KINDS: frozenset[SourceKind] = frozenset(
    {SourceKind.peer_reviewed, SourceKind.usgs_comcat}
)
"""Source kinds that may support a `single_force=True` seismic attribution."""


class SourceRef(BaseModel):
    """A document that was actually retrieved, hashed and read in-session.

    `sha256` is the digest of the bytes that were retrieved (for a paywalled paper this is the
    landing page, and `stored_copy` is None). `claims_supported` lists the dotted field paths
    of the record that this source backs.
    """

    model_config = DOMAIN_CONFIG

    id: Slug
    kind: SourceKind
    title: str = Field(min_length=1)
    url: str = Field(
        pattern=URL_PATTERN,
        description=(
            "The URL whose bytes produced `sha256`. A reader who fetches this and hashes it "
            "must get `sha256`; put a human-facing landing page in `related_url` instead."
        ),
    )
    related_url: str | None = Field(
        default=None,
        pattern=URL_PATTERN,
        description="Landing page for a reader, when it differs from the bytes that were hashed.",
    )
    doi: str | None = Field(default=None, pattern=DOI_PATTERN)
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1800, le=2100)
    publisher: str | None = None
    accessed_utc: AwareDatetime
    sha256: Sha256
    content_type: str = Field(min_length=1)
    licence: str = Field(min_length=1, description="SPDX id or the licence as stated")
    stored_copy: str | None = Field(
        default=None, description="Repo-relative path of a stored copy, if the licence allows"
    )
    claims_supported: list[FieldPath] = Field(min_length=1)
    excerpt: str | None = Field(default=None, max_length=300)
    peer_reviewed: bool

    @model_validator(mode="after")
    def _kind_matches_flag(self) -> Self:
        is_peer = self.kind == SourceKind.peer_reviewed
        if is_peer != self.peer_reviewed:
            raise ValueError(
                f"{self.id}: peer_reviewed={self.peer_reviewed} disagrees with kind={self.kind}"
            )
        return self


class AttributedEstimate(BaseModel):
    """One published figure, attributed to exactly one source."""

    model_config = DOMAIN_CONFIG

    low: float = Field(allow_inf_nan=False)
    high: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1)
    source_ref: Slug
    qualifier: str | None = Field(
        default=None, description="e.g. 'order of magnitude', 'preliminary', 'bed erosion only'"
    )

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low > self.high:
            raise ValueError(f"estimate low={self.low} exceeds high={self.high}")
        return self


class Range(BaseModel):
    """A sourced numeric interval. Every number in an event record is one of these.

    `best` is optional and may only be set when the owning record can show a qualifying source
    (see `BEST_QUALIFYING_KINDS`); the aggregate enforces that because a `Range` cannot see the
    `sources[]` list on its own.
    """

    model_config = DOMAIN_CONFIG

    low: float = Field(allow_inf_nan=False)
    high: float = Field(allow_inf_nan=False)
    best: float | None = Field(default=None, allow_inf_nan=False)
    unit: str = Field(min_length=1)
    source_refs: list[Slug] = Field(min_length=1)
    disputed: bool = False
    estimates: list[AttributedEstimate] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        if self.low > self.high:
            problems.append(f"low={self.low} exceeds high={self.high}")
        if self.best is not None and not (self.low <= self.best <= self.high):
            problems.append(f"best={self.best} outside [{self.low}, {self.high}]")
        if len(set(self.source_refs)) != len(self.source_refs):
            problems.append("source_refs contains duplicates")
        for i, est in enumerate(self.estimates):
            if est.unit != self.unit:
                problems.append(f"estimates[{i}].unit={est.unit!r} differs from unit={self.unit!r}")
        if self.disputed:
            if self.best is not None:
                problems.append("disputed range cannot carry best")
            if len(self.estimates) < 2:
                problems.append("disputed range needs at least 2 attributed estimates")
            if not self.notes:
                problems.append("disputed range needs notes")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class FieldNoteReason(StrEnum):
    """Why a nullable numeric field is null."""

    no_peer_reviewed_estimate = "no_peer_reviewed_estimate"
    not_applicable = "not_applicable"
    not_yet_researched = "not_yet_researched"
    disputed_beyond_range = "disputed_beyond_range"
    not_public = "not_public"


class FieldNote(BaseModel):
    """Explanation attached to a null field; carries the attributed public estimates, if any."""

    model_config = DOMAIN_CONFIG

    reason: FieldNoteReason
    public_estimates: list[AttributedEstimate] = Field(default_factory=list)
    notes: str = Field(min_length=20)


class GeometryQuality(StrEnum):
    """How a geometry was obtained; surfaced by validation and the release ledger."""

    surveyed = "surveyed"
    snapped_to_osm_centreline = "snapped_to_osm_centreline"
    osm_centreline = "osm_centreline"
    osm_node = "osm_node"
    hand_digitised_approximate = "hand_digitised_approximate"
    osm_way_centroid = "osm_way_centroid"
    source_stated_location = "source_stated_location"


class RecordMeta(BaseModel):
    """Who wrote a record and whether anyone has reviewed it."""

    model_config = DOMAIN_CONFIG

    created_utc: AwareDatetime
    created_by: str = Field(min_length=1)
    reviewed_by: str | None = None
    review_utc: AwareDatetime | None = None

    @model_validator(mode="after")
    def _review_pair(self) -> Self:
        if (self.reviewed_by is None) != (self.review_utc is None):
            raise ValueError("reviewed_by and review_utc must be set together")
        if self.review_utc is not None and self.review_utc < self.created_utc:
            raise ValueError("review_utc precedes created_utc")
        return self


# --- generic tree walkers -------------------------------------------------------------------


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _walk(value: object, path: str) -> Iterator[tuple[str, BaseModel]]:
    if isinstance(value, BaseModel):
        yield path, value
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name), _join(path, name))
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _walk(item, f"{path}[{i}]")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, _join(path, str(key)))


def iter_models(model: BaseModel) -> Iterator[tuple[str, BaseModel]]:
    """Yield `(dotted_path, submodel)` for the model and every nested model, depth first.

    Lists are indexed as `field[0]`, dict entries as `field.key`. The root has path `""`.
    """
    yield from _walk(model, "")


def iter_ranges(model: BaseModel) -> Iterator[tuple[str, Range]]:
    """Yield `(dotted_path, Range)` for every `Range` anywhere in a model tree."""
    for path, sub in iter_models(model):
        if isinstance(sub, Range):
            yield path, sub


def annotation_accepts(annotation: object, cls: type) -> bool:
    """True if a field annotation is `cls` or a union that includes `cls`."""
    if annotation is cls:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return annotation_accepts(get_args(annotation)[0], cls)
    if origin is Union or origin is types.UnionType:
        return any(annotation_accepts(arg, cls) for arg in get_args(annotation))
    return False


def iter_none_fields(model: BaseModel) -> Iterator[tuple[str, Any]]:
    """Yield `(dotted_path, annotation)` for every field in the tree whose value is None."""
    for path, sub in iter_models(model):
        for name, info in type(sub).model_fields.items():
            if getattr(sub, name) is None:
                yield _join(path, name), info.annotation


def iter_field_paths(model: BaseModel) -> Iterator[str]:
    """Yield the dotted path of every field of every model in the tree."""
    for path, sub in iter_models(model):
        for name in type(sub).model_fields:
            yield _join(path, name)


def iter_source_ref_ids(model: BaseModel) -> Iterator[tuple[str, str]]:
    """Yield `(dotted_path, source_id)` for every `source_refs[i]` / `source_ref` in the tree."""
    for path, sub in iter_models(model):
        fields = type(sub).model_fields
        if "source_refs" in fields:
            refs = getattr(sub, "source_refs", None)
            if isinstance(refs, list):
                for i, ref in enumerate(refs):
                    if isinstance(ref, str):
                        yield f"{_join(path, 'source_refs')}[{i}]", ref
        if "source_ref" in fields:
            ref = getattr(sub, "source_ref", None)
            if isinstance(ref, str):
                yield _join(path, "source_ref"), ref


CONTRACTS: dict[str, type[BaseModel]] = {"source-ref": SourceRef}
