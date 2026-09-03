"""Assemble the discriminator's three-class catalogue: positives, negatives, noise.

The split unit is the **event group**, never the window. Every negative and every noise window
inherits its matched positive's `event_group`, so a group cannot straddle train and test no
matter how the splitter is written. That inheritance is the single mechanism that makes the
nine leakage assertions in `serac.validation.discriminator` checkable rather than aspirational.

Three joins produce the positives: the ESEC catalogue (the bulk of them), USGS ComCat
`eventtype=landslide`, and the serac event library. Records within +/-180 s and 100 km of each
other are one physical event and are merged, keeping every contributing id, because the same
failure appears in more than one catalogue with slightly different coordinates and times.

**Negative matching.** The brief asks for tectonic negatives "matched by magnitude, region and
epoch". ESEC publishes no magnitude for its events, so magnitude matching between a positive
and its negatives is not available and is not faked. What is matched instead, and why:

* **the station set** — negatives are windowed at exactly the stations the positive was
  windowed at (`windows.py` enforces this). Without it the model learns station identity: the
  positives would come from one set of instruments and the negatives from another, and a
  classifier that memorised which is which would score perfectly while knowing no physics.
* **epicentral proximity** (default 400 km) — so path length, crustal structure and the
  distance range spanned by the shared station set are comparable.
* **epoch** (default +/-2 years) — so instrument generations, noise conditions and network
  geometry are comparable.
* **an absolute magnitude window** (default 4.0-6.5) — large enough to be visible across a
  100-1500 km annulus, small enough to avoid clipping and complex ruptures. This is a fixed
  band, not a per-positive match, and is reported as such.

**Noise windows** are drawn at a fixed offset before the positive's origin at the same
stations, and are dropped if any catalogued earthquake of M>=4 falls inside them. They are
labelled `noise` meaning "no catalogued source", not "quiet": real ambient noise, teleseisms
below the screen, and cultural noise are all in this class, which is the point.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from serac.adapters.seismic.esec import EsecEvent, load_esec_fixture
from serac.errors import SeracError
from serac.models.discriminator.regions import region_for
from serac.ports.seismic import CatalogEvent

CATALOG_VERSION = "0.1.0"

EARTH_RADIUS_KM = 6371.0088

# Events from different catalogues within both of these of each other are one event.
DEDUPE_SECONDS = 180.0
DEDUPE_KM = 100.0

# Groups that must land in the test set and in neither train nor validation. These are the
# events M1 exists for; scoring them after any exposure, including early stopping or a
# threshold choice, would make every number about them meaningless.
FORCED_TEST_GROUPS: frozenset[str] = frozenset(
    {"chamoli-2021", "langtang-lhende-2026", "us7000tbwb", "us7000tc90"}
)

NEGATIVES_PER_POSITIVE = 5
NEGATIVE_MAX_DISTANCE_KM = 400.0
NEGATIVE_EPOCH_YEARS = 2.0
NEGATIVE_MIN_MAGNITUDE = 4.0
NEGATIVE_MAX_MAGNITUDE = 6.5
# A tectonic event closer in time than this to the mass movement may be its trigger or its
# response; excluding them keeps the two classes physically distinct.
NEGATIVE_MIN_TIME_SEPARATION_S = 6 * 3600.0

NOISE_PER_POSITIVE = 1
NOISE_OFFSET_S = 6 * 3600.0
NOISE_EXCLUSION_MIN_MAGNITUDE = 4.0

WINDOW_PRE_ORIGIN_S = 60.0
WINDOW_LENGTH_S = 600.0


class CatalogError(SeracError):
    """The discriminator catalogue could not be assembled."""


class ClassLabel(StrEnum):
    """The three classes the discriminator separates."""

    mass_movement = "mass_movement"
    tectonic = "tectonic"
    noise = "noise"


class CatalogSource(StrEnum):
    """Which catalogue a row came from."""

    esec = "esec"
    comcat_landslide = "comcat_landslide"
    event_library = "event_library"
    comcat_tectonic = "comcat_tectonic"
    noise_window = "noise_window"


class CatalogEntry(BaseModel):
    """One labelled 600 s window request, before any station is chosen or byte fetched."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str = Field(min_length=1)
    event_group: str = Field(
        min_length=1,
        description="The split unit. Negatives and noise inherit their positive's group.",
    )
    class_label: ClassLabel
    origin_utc: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    region_id: str = Field(min_length=1)
    source: CatalogSource
    source_ids: list[str] = Field(
        min_length=1, description="Every catalogue id that contributed, e.g. `esec:274`."
    )
    magnitude: float | None = Field(
        default=None, description="Null for ESEC positives: the catalogue publishes none."
    )
    magnitude_type: str | None = None
    sub_type: str | None = Field(default=None, description="ESEC `SubType`, for the model card.")
    description: str = ""
    matched_positive_id: str | None = Field(
        default=None, description="Set on every negative and noise row; null on positives."
    )
    location_basis: str = Field(min_length=1)

    @property
    def window_start_utc(self) -> datetime:
        return self.origin_utc - timedelta(seconds=WINDOW_PRE_ORIGIN_S)

    @property
    def window_end_utc(self) -> datetime:
        return self.window_start_utc + timedelta(seconds=WINDOW_LENGTH_S)

    @property
    def decade(self) -> str:
        return f"{self.origin_utc.year // 10 * 10}s"

    @model_validator(mode="after")
    def _inheritance(self) -> Self:
        if self.class_label is ClassLabel.mass_movement:
            if self.matched_positive_id is not None:
                raise ValueError("a positive must not carry matched_positive_id")
        elif self.matched_positive_id is None:
            raise ValueError(
                f"{self.class_label} row {self.entry_id} has no matched_positive_id; negatives "
                "and noise must inherit a positive's split"
            )
        return self


class DiscriminatorCatalog(BaseModel):
    """The assembled catalogue plus the provenance of how it was assembled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = CATALOG_VERSION
    built_at_utc: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    entries: list[CatalogEntry]
    positives_before_dedupe: int = Field(ge=0)
    positives_after_dedupe: int = Field(ge=0)
    dedupe_merges: int = Field(ge=0)
    notes: list[str] = Field(default_factory=list)

    def by_class(self, label: ClassLabel) -> list[CatalogEntry]:
        return [e for e in self.entries if e.class_label is label]

    @property
    def groups(self) -> set[str]:
        return {e.event_group for e in self.entries}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


class _RawPositive(BaseModel):
    """A positive as one catalogue states it, before the cross-catalogue merge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: CatalogSource
    source_id: str
    preferred_group: str | None
    origin_utc: AwareDatetime
    latitude: float
    longitude: float
    location_basis: str
    description: str
    sub_type: str | None = None
    magnitude: float | None = None
    magnitude_type: str | None = None


def _from_esec(events: Iterable[EsecEvent]) -> list[_RawPositive]:
    out = []
    for e in events:
        lat, lon = e.location
        out.append(
            _RawPositive(
                source=CatalogSource.esec,
                source_id=f"esec:{e.event_id}",
                preferred_group=None,
                origin_utc=e.start_utc,
                latitude=lat,
                longitude=lon,
                location_basis=e.location_basis,
                description=f"{e.name} — {e.type}".strip(" —"),
                sub_type=e.sub_type.value,
            )
        )
    return out


def _from_comcat_landslides(events: Iterable[CatalogEvent]) -> list[_RawPositive]:
    return [
        _RawPositive(
            source=CatalogSource.comcat_landslide,
            source_id=f"comcat:{e.event_id}",
            preferred_group=e.event_id,
            origin_utc=e.time_utc,
            latitude=e.latitude,
            longitude=e.longitude,
            location_basis="usgs_comcat_epicentre",
            description=e.title or e.event_id,
            magnitude=e.magnitude,
            magnitude_type=e.mag_type,
        )
        for e in events
    ]


def load_event_library_positives(repo_root: Path) -> list[_RawPositive]:
    """Serac event-library records that are mass movements with a usable origin time.

    `south-lhonak-2023` is the library's `negative_control` (a moraine-dam collapse GLOF) and
    is excluded: it is not the single-force signal M1 discriminates. Records whose `time` is
    midnight with a date-only basis carry no usable origin second and are excluded too — a
    600 s window around a guessed midnight would be a fabricated observation.
    """
    out: list[_RawPositive] = []
    for path in sorted((repo_root / "data" / "events").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("role") == "negative_control":
            continue
        time_block = record.get("time") or {}
        raw_time = time_block.get("datetime_utc")
        location = record.get("source_location") or {}
        lat, lon = location.get("lat"), location.get("lon")
        if not raw_time or lat is None or lon is None:
            continue
        origin = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if origin.tzinfo is None:
            origin = origin.replace(tzinfo=UTC)
        if origin.hour == 0 and origin.minute == 0 and origin.second == 0:
            # Date-only records (Aru Co, Kolka-Karmadon, Sedongpu 2018) have no origin second.
            continue
        out.append(
            _RawPositive(
                source=CatalogSource.event_library,
                source_id=f"serac:{record['event_id']}",
                preferred_group=str(record.get("event_group") or record["event_id"]),
                origin_utc=origin,
                latitude=float(lat),
                longitude=float(lon),
                location_basis=str(time_block.get("basis") or "event_library"),
                description=str(record.get("name") or record["event_id"]),
                sub_type=str(record.get("failure_type") or ""),
            )
        )
    return out


def _merge_clusters(raws: Sequence[_RawPositive]) -> list[list[_RawPositive]]:
    """Single-link clustering under the +/-180 s and 100 km rule, in time order."""
    ordered = sorted(raws, key=lambda r: (r.origin_utc, r.source_id))
    clusters: list[list[_RawPositive]] = []
    for raw in ordered:
        for cluster in reversed(clusters):
            if any(
                abs((raw.origin_utc - other.origin_utc).total_seconds()) <= DEDUPE_SECONDS
                and haversine_km(raw.latitude, raw.longitude, other.latitude, other.longitude)
                <= DEDUPE_KM
                for other in cluster
            ):
                cluster.append(raw)
                break
        else:
            clusters.append([raw])
    return clusters


def _entry_from_cluster(cluster: Sequence[_RawPositive]) -> CatalogEntry:
    """Merge one cluster, preferring the serac event library, then ComCat, then ESEC.

    The library is preferred because its coordinates and times were sourced and reviewed one
    by one; ComCat next because it is an operational catalogue with a stated location basis;
    ESEC last because its coordinates are a mixture of crown and nominal points.
    """
    order = {
        CatalogSource.event_library: 0,
        CatalogSource.comcat_landslide: 1,
        CatalogSource.esec: 2,
    }
    primary = min(cluster, key=lambda r: (order[r.source], r.source_id))
    group = primary.preferred_group or f"esec-{primary.source_id.split(':', 1)[1]}"
    sub_type = next((r.sub_type for r in cluster if r.sub_type), None)
    # The id is per catalogue record, the group is per split unit: Sedongpu 2017 and 2018 are
    # two records that must share a group (they are the same slope) but not an id.
    return CatalogEntry(
        entry_id=f"pos/{primary.source_id.replace(':', '-')}",
        event_group=group,
        class_label=ClassLabel.mass_movement,
        origin_utc=primary.origin_utc,
        latitude=primary.latitude,
        longitude=primary.longitude,
        region_id=region_for(primary.latitude, primary.longitude),
        source=primary.source,
        source_ids=sorted({r.source_id for r in cluster}),
        magnitude=primary.magnitude,
        magnitude_type=primary.magnitude_type,
        sub_type=sub_type,
        description=primary.description,
        location_basis=primary.location_basis,
    )


def build_positives(
    repo_root: Path,
    *,
    comcat_landslides: Sequence[CatalogEvent] = (),
) -> tuple[list[CatalogEntry], int, int]:
    """(positives, count before dedupe, number of merges)."""
    raws = (
        _from_esec(load_esec_fixture(repo_root))
        + _from_comcat_landslides(comcat_landslides)
        + load_event_library_positives(repo_root)
    )
    clusters = _merge_clusters(raws)
    entries = [_entry_from_cluster(c) for c in clusters]
    entries.sort(key=lambda e: (e.origin_utc, e.entry_id))
    merges = sum(len(c) - 1 for c in clusters)
    return entries, len(raws), merges


def match_negatives(
    positive: CatalogEntry,
    tectonic: Sequence[CatalogEvent],
    *,
    per_positive: int = NEGATIVES_PER_POSITIVE,
) -> list[CatalogEntry]:
    """Tectonic negatives for one positive, inheriting its `event_group`.

    Candidates are ranked by epicentral distance so the chosen negatives illuminate the same
    paths to the same stations. `per_positive` may not be met; the shortfall is a reported
    number, never made up by relaxing the window.
    """
    horizon = timedelta(days=365.25 * NEGATIVE_EPOCH_YEARS)
    candidates = []
    for event in tectonic:
        if event.magnitude is None:
            continue
        if not NEGATIVE_MIN_MAGNITUDE <= event.magnitude <= NEGATIVE_MAX_MAGNITUDE:
            continue
        separation = abs((event.time_utc - positive.origin_utc).total_seconds())
        if separation < NEGATIVE_MIN_TIME_SEPARATION_S:
            continue
        if event.time_utc < positive.origin_utc - horizon:
            continue
        if event.time_utc > positive.origin_utc + horizon:
            continue
        distance = haversine_km(
            positive.latitude, positive.longitude, event.latitude, event.longitude
        )
        if distance > NEGATIVE_MAX_DISTANCE_KM:
            continue
        candidates.append((distance, event))
    candidates.sort(key=lambda pair: (pair[0], pair[1].event_id))

    out = []
    for distance, event in candidates[:per_positive]:
        out.append(
            CatalogEntry(
                entry_id=f"neg/{positive.event_group}/{event.event_id}",
                event_group=positive.event_group,
                class_label=ClassLabel.tectonic,
                origin_utc=event.time_utc,
                latitude=event.latitude,
                longitude=event.longitude,
                region_id=positive.region_id,
                source=CatalogSource.comcat_tectonic,
                source_ids=[f"comcat:{event.event_id}"],
                magnitude=event.magnitude,
                magnitude_type=event.mag_type,
                description=(
                    f"{event.title or event.event_id} "
                    f"({distance:.0f} km from {positive.event_group})"
                ),
                matched_positive_id=positive.entry_id,
                location_basis="usgs_comcat_epicentre",
            )
        )
    return out


def make_noise_windows(
    positive: CatalogEntry,
    tectonic: Sequence[CatalogEvent],
    *,
    per_positive: int = NOISE_PER_POSITIVE,
) -> list[CatalogEntry]:
    """Pre-origin noise windows at the positive's own stations, inheriting its group.

    A window is dropped when any catalogued M>=4 earthquake anywhere falls inside it, because
    a teleseism in a `noise` window would teach the model that tectonic energy is noise.
    """
    out = []
    for index in range(per_positive):
        origin = positive.origin_utc - timedelta(seconds=NOISE_OFFSET_S * (index + 1))
        start = origin - timedelta(seconds=WINDOW_PRE_ORIGIN_S)
        end = start + timedelta(seconds=WINDOW_LENGTH_S)
        contaminated = any(
            e.magnitude is not None
            and e.magnitude >= NOISE_EXCLUSION_MIN_MAGNITUDE
            and start <= e.time_utc <= end
            for e in tectonic
        )
        if contaminated:
            continue
        out.append(
            CatalogEntry(
                entry_id=f"noise/{positive.event_group}/{index}",
                event_group=positive.event_group,
                class_label=ClassLabel.noise,
                origin_utc=origin,
                latitude=positive.latitude,
                longitude=positive.longitude,
                region_id=positive.region_id,
                source=CatalogSource.noise_window,
                source_ids=[f"noise:{positive.event_group}:{index}"],
                description=(
                    f"No catalogued source; {NOISE_OFFSET_S * (index + 1) / 3600:.0f} h before "
                    f"{positive.event_group} at the same stations"
                ),
                matched_positive_id=positive.entry_id,
                location_basis="inherited_from_matched_positive",
            )
        )
    return out


def assemble(
    repo_root: Path,
    *,
    comcat_landslides: Sequence[CatalogEvent] = (),
    tectonic_by_positive: dict[str, Sequence[CatalogEvent]] | None = None,
) -> DiscriminatorCatalog:
    """Positives, their matched negatives and their noise windows, as one catalogue."""
    positives, before, merges = build_positives(repo_root, comcat_landslides=comcat_landslides)
    tectonic_by_positive = tectonic_by_positive or {}
    entries = list(positives)
    shortfalls = 0
    for positive in positives:
        tectonic = tectonic_by_positive.get(positive.entry_id, ())
        negatives = match_negatives(positive, tectonic)
        if len(negatives) < NEGATIVES_PER_POSITIVE:
            shortfalls += 1
        entries.extend(negatives)
        entries.extend(make_noise_windows(positive, tectonic))
    notes = [
        f"positives {before} raw -> {len(positives)} after +/-{DEDUPE_SECONDS:.0f} s / "
        f"{DEDUPE_KM:.0f} km dedupe ({merges} merged)",
        f"{shortfalls} positives matched fewer than {NEGATIVES_PER_POSITIVE} negatives; the "
        "shortfall is reported, not filled by widening the match window",
        "ESEC publishes no magnitude, so negatives are matched on station set, epicentral "
        f"proximity (<= {NEGATIVE_MAX_DISTANCE_KM:.0f} km) and epoch "
        f"(+/-{NEGATIVE_EPOCH_YEARS:.0f} y) within a fixed M{NEGATIVE_MIN_MAGNITUDE}"
        f"-{NEGATIVE_MAX_MAGNITUDE} band, not on per-event magnitude",
    ]
    return DiscriminatorCatalog(
        entries=entries,
        positives_before_dedupe=before,
        positives_after_dedupe=len(positives),
        dedupe_merges=merges,
        notes=notes,
    )


# --- tectonic candidate acquisition -------------------------------------------------------

COMCAT_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
# Teleseisms this large dominate a regional record anywhere on Earth, so a noise window
# containing one is not noise. Checked globally; the local M>=4 screen covers the rest.
GLOBAL_TELESEISM_MIN_MAGNITUDE = 6.0


def comcat_circle_params(
    latitude: float,
    longitude: float,
    start_utc: datetime,
    end_utc: datetime,
    *,
    radius_km: float,
    min_magnitude: float,
    max_magnitude: float | None = None,
) -> dict[str, str]:
    """fdsnws-event circle-search parameters. Returned separately so they can be ledgered."""
    params = {
        "format": "geojson",
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "maxradiuskm": f"{radius_km:.1f}",
        "starttime": start_utc.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "endtime": end_utc.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "minmagnitude": f"{min_magnitude:.2f}",
        "eventtype": "earthquake",
        "orderby": "time-asc",
        "limit": "20000",
    }
    if max_magnitude is not None:
        params["maxmagnitude"] = f"{max_magnitude:.2f}"
    return params


def tectonic_search_window(positive: CatalogEntry) -> tuple[datetime, datetime]:
    horizon = timedelta(days=365.25 * NEGATIVE_EPOCH_YEARS)
    return positive.origin_utc - horizon, positive.origin_utc + horizon
