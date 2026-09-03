"""The published force histories and masses serac reproduces, and how they were obtained.

`data/references/lfh_published.json` is the only place a published number may enter the
force-history work, and it enters under the repository's citation rule (CLAUDE.md): the DOI
was resolved through Crossref or DataCite in the same session, the bytes carrying the number
were fetched, and the sha256 of those bytes plus `accessed_utc` are recorded. Every published
figure carries the **verbatim sentence** it came from, so a reader can check the reading
without refetching.

Two consequences are enforced rather than documented:

* `validate-lfh` counts references that clear that bar. Fewer than three and the gate
  **fails** with `published_refs_fetched=False`. It does not pass on two, and never on a
  number recalled from memory.
* Where the published quantity is not the quantity serac computes -- a volume rather than a
  mass, most often -- a `Conversion` block states the arithmetic and the assumption, and the
  comparison is reported as converted rather than as published.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

LFH_REFERENCES_VERSION = "0.1.0"
REFERENCES_PATH = Path("data/references/lfh_published.json")

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class SourceRef(BaseModel):
    """A source that was actually fetched, hashed and DOI-resolved in session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: Literal["peer_reviewed", "dataset", "agency_official", "preprint"]
    title: str = Field(min_length=1)
    container: str | None = None
    authors: str | None = None
    year: int | None = None
    doi: str | None = None
    doi_resolved_via: Literal["crossref", "datacite", "publisher_landing_page"] | None = None
    doi_resolution_url: str | None = None
    url: str = Field(
        min_length=1,
        description=(
            "The URL whose bytes produced `sha256`. A reader who fetches this and hashes it "
            "must get `sha256`. Two ESEC sources once recorded a per-event landing page here "
            "while the digest was of the whole-catalogue response, so re-fetching the url gave "
            "a different hash and the provenance was unverifiable. A human-facing page that is "
            "not the hashed bytes belongs in `related_url`."
        ),
    )
    related_url: str | None = Field(
        default=None,
        description="Landing page for a reader, when it differs from the bytes that were hashed.",
    )
    accessed_utc: AwareDatetime
    sha256: str = Field(pattern=SHA256_PATTERN, description="sha256 of the bytes retrieved.")
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    licence: str = Field(min_length=1)
    peer_reviewed: bool

    @property
    def clears_citation_bar(self) -> bool:
        """Fetched, hashed, and with a DOI resolved in session. All three or it does not count."""
        return bool(self.doi and self.doi_resolved_via and self.sha256 and self.accessed_utc)

    def citation(self) -> str:
        bits = [self.authors or "", f"({self.year})" if self.year else "", self.title]
        if self.container:
            bits.append(self.container)
        if self.doi:
            bits.append(f"doi:{self.doi}")
        return " ".join(b for b in bits if b)


class PublishedQuantity(BaseModel):
    """One published number or interval, with the sentence it was read from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low: float
    high: float
    best: float | None = None
    units: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, description="Verbatim sentence carrying the number.")
    notes: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low > self.high:
            raise ValueError(f"published interval must be ordered: {self.low} > {self.high}")
        if self.best is not None and not self.low <= self.best <= self.high:
            raise ValueError("published best must lie within [low, high]")
        return self


class Conversion(BaseModel):
    """How a published quantity was turned into the one serac computes.

    Present only when the published figure is not directly comparable. The factor and its
    justification are recorded so the comparison is auditable and so nobody mistakes the
    converted interval for something a paper printed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_quantity: str = Field(min_length=1)
    to_quantity: str = Field(min_length=1)
    factor_low: float = Field(gt=0)
    factor_high: float = Field(gt=0)
    factor_units: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.factor_low > self.factor_high:
            raise ValueError("conversion factor interval must be ordered")
        return self


class LfhTarget(BaseModel):
    """One event serac inverts: where it was, what was published, what it stands on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: Literal["reproduction", "new_event"]
    origin_utc: AwareDatetime
    source_latitude: float = Field(ge=-90, le=90)
    source_longitude: float = Field(ge=-180, le=180)
    event_id: str | None = Field(
        default=None, description="`data/events/` record, when one exists."
    )
    aoi_id: str | None = None
    fixture_dir: str = Field(min_length=1)
    dem_fixture: str | None = None
    fall_height_m: float | None = Field(default=None, gt=0)
    runout_m: float | None = Field(default=None, gt=0)
    geometry_source_ref: str | None = None
    published_mass_kg: PublishedQuantity | None = None
    published_peak_force_n: PublishedQuantity | None = None
    published_duration_s: PublishedQuantity | None = None
    published_volume_m3: PublishedQuantity | None = None
    #: The direction the mass travelled, in degrees from north. Compared against serac's
    #: `force_azimuth_deg` interval rotated by 180 degrees, since the force a slide exerts
    #: points opposite its motion. It is the strongest independent check available: nothing in
    #: the inversion is fitted to a bearing, and a wrong transverse sign or a mirrored
    #: `F_north = -Ft` convention would move it by 180 or reflect it about the meridian.
    published_runout_bearing_deg: PublishedQuantity | None = None
    mass_conversion: Conversion | None = None
    public_statements: list[str] = Field(
        default_factory=list,
        description="Attributed public figures for the Disagreement section; never a target.",
    )
    notes: str | None = None

    def comparison_mass_kg(self) -> tuple[float, float, str] | None:
        """`(low, high, provenance)` for the mass the reproduction gate compares against.

        Either the published mass itself, or a published volume converted with a stated
        density interval. Returns None when neither exists, which is not a failure -- it means
        this target is not a mass reproduction.
        """
        if self.published_mass_kg is not None:
            return (
                self.published_mass_kg.low,
                self.published_mass_kg.high,
                f"published mass, {self.published_mass_kg.source_ref}",
            )
        if self.published_volume_m3 is not None and self.mass_conversion is not None:
            conversion = self.mass_conversion
            return (
                self.published_volume_m3.low * conversion.factor_low,
                self.published_volume_m3.high * conversion.factor_high,
                (
                    f"published volume ({self.published_volume_m3.source_ref}) converted with "
                    f"{conversion.factor_low:g}-{conversion.factor_high:g} "
                    f"{conversion.factor_units}"
                ),
            )
        return None


class LfhReferences(BaseModel):
    """The whole reference file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = LFH_REFERENCES_VERSION
    generated_at_utc: AwareDatetime
    citation_rule: str = Field(min_length=1)
    sources: list[SourceRef]
    targets: list[LfhTarget]

    @model_validator(mode="after")
    def _refs_resolve(self) -> Self:
        known = {source.id for source in self.sources}
        for target in self.targets:
            referenced = [
                quantity.source_ref
                for quantity in (
                    target.published_mass_kg,
                    target.published_peak_force_n,
                    target.published_duration_s,
                    target.published_volume_m3,
                    target.published_runout_bearing_deg,
                )
                if quantity is not None
            ]
            if target.geometry_source_ref:
                referenced.append(target.geometry_source_ref)
            missing = sorted(set(referenced) - known)
            if missing:
                raise ValueError(f"{target.target_id} cites unknown sources: {missing}")
        return self

    @property
    def sources_clearing_bar(self) -> list[SourceRef]:
        return [source for source in self.sources if source.clears_citation_bar]

    def source(self, source_id: str) -> SourceRef:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def target(self, target_id: str) -> LfhTarget:
        for target in self.targets:
            if target.target_id == target_id:
                return target
        raise KeyError(target_id)

    @property
    def reproductions(self) -> list[LfhTarget]:
        return [t for t in self.targets if t.role == "reproduction"]

    @property
    def new_events(self) -> list[LfhTarget]:
        return [t for t in self.targets if t.role == "new_event"]


def load_references(repo: Path) -> LfhReferences:
    path = repo / REFERENCES_PATH
    if not path.exists():
        raise FileNotFoundError(f"no LFH reference file at {path}")
    return LfhReferences.model_validate_json(path.read_text(encoding="utf-8"))


def write_references(references: LfhReferences, repo: Path) -> Path:
    path = repo / REFERENCES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(references.model_dump_json())
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def origin_of(target: LfhTarget) -> datetime:
    return target.origin_utc
