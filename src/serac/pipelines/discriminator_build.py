"""Build the discriminator training set: `serac data build-discriminator-set`.

Four phases, each resumable, each writing its bytes to disk and a row to
`data/manifest.jsonl` before the next begins:

1. **Tectonic candidates.** One fdsnws-event circle query per positive; the raw GeoJSON is
   stored verbatim. Negatives are matched from these, never invented.
2. **Station selection.** One fdsnws-station query per positive, open channels only, in the
   100-1500 km annulus. The chosen stations belong to the positive's **group**, and its
   negatives and noise windows are cut at exactly those stations.
3. **Waveforms and responses.** One bulk dataselect request per window; one level=response
   StationXML per station, cached across the ~7 windows that share it.
4. **Processing.** Response removal to velocity, bandpass, resample, into Zarr, then the
   sorted chunk-hash index whose sha256 goes in the ledger.

**An event with no data is recorded, not replaced.** A window whose stations return nothing,
or fewer than `MIN_STATIONS_PER_WINDOW` usable channels, gets a `status: not_fetched` ledger
row carrying the reason and is excluded from the dataset. It is never substituted with another
event, backfilled from a neighbour, or quietly dropped: the count of such events is a headline
number in the model card, because it is the difference between the catalogue serac wanted and
the dataset it actually has.
"""

from __future__ import annotations

import json
import random
import time
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from serac.adapters.storage.manifest_ledger import sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.errors import SeracError
from serac.models.discriminator import catalog as cat
from serac.models.discriminator.catalog import (
    COMCAT_QUERY_URL,
    NEGATIVE_MAX_DISTANCE_KM,
    NEGATIVE_MIN_MAGNITUDE,
    CatalogEntry,
    ClassLabel,
    DiscriminatorCatalog,
)
from serac.models.discriminator.dataset import (
    DatasetIndex,
    make_record,
    open_store,
    write_chunk_index,
    write_index,
    write_window,
)
from serac.models.discriminator.windows import (
    BANDPASS_HZ,
    COMPONENTS,
    ESTIMATE_BASIS,
    MAX_STATIONS_PER_EVENT,
    MIN_STATIONS_PER_WINDOW,
    N_SAMPLES,
    TARGET_SAMPLING_RATE_HZ,
    MissingResponseError,
    StationChoice,
    bulk_rows_for,
    estimate_window_bytes,
    process_station_window,
    select_stations,
)
from serac.ports.ledger import ManifestLedger
from serac.ports.seismic import CatalogEvent

BUILD_VERSION = "0.1.0"
ADAPTER_NAME = "DiscriminatorSetBuilder"

DATA_CENTRES: dict[str, str] = {
    "earthscope": "https://service.earthscope.org",
    "geofon": "https://geofon.gfz.de",
}
KM_PER_DEGREE = 111.19492664455873

LICENCE_NULL = "null: see licence_source_url"
FDSN_TERMS_URL = "https://www.earthscope.org/terms-of-service/"
FDSN_NOTE = (
    "EarthScope Terms of Service state no licence; users must acknowledge EarthScope "
    "Consortium (NSF award 2435260) and cite network DOIs. GEOFON data are served under the "
    "GEOFON data policy; both are recorded as licence null with the terms URL."
)
COMCAT_LICENCE = "US-PD"
COMCAT_LICENCE_URL = (
    "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits"
)

# The ask-first threshold from CLAUDE.md rule 7, applied to the whole build.
CONFIRM_ABOVE_BYTES = 5 * 1000**3

INTERIM = Path("data/interim/discriminator")
RAW = Path("data/raw/discriminator")


class BuildError(SeracError):
    """The discriminator set could not be built."""


class BuildPlan(BaseModel):
    """Dry-run description: what would be fetched, how big, and on what stated assumption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    n_positives: int = Field(ge=0)
    n_negatives: int = Field(ge=0)
    n_noise: int = Field(ge=0)
    n_windows: int = Field(ge=0)
    n_groups: int = Field(ge=0)
    n_unique_stations: int = Field(ge=0)
    estimated_waveform_bytes: int = Field(ge=0)
    estimated_response_bytes: int = Field(ge=0)
    estimated_zarr_bytes: int = Field(ge=0)
    estimated_total_bytes: int = Field(ge=0)
    estimate_basis: str
    class_by_region: dict[str, dict[str, int]] = Field(default_factory=dict)
    class_by_decade: dict[str, dict[str, int]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return self.estimated_total_bytes > CONFIRM_ABOVE_BYTES


class BuildReport(BaseModel):
    """What the build actually did, including everything it could not fetch."""

    model_config = ConfigDict(extra="forbid")

    built_at_utc: datetime
    n_windows_requested: int = 0
    n_windows_written: int = 0
    n_windows_not_fetched: int = 0
    not_fetched: dict[str, str] = Field(default_factory=dict)
    positives_requested: int = 0
    positives_written: int = 0
    bytes_fetched: int = 0
    chunk_index_sha256: str | None = None
    n_chunk_files: int = 0
    notes: list[str] = Field(default_factory=list)


@dataclass
class _Cache:
    """Filesystem cache roots for a resumable build."""

    repo: Path

    @property
    def tectonic(self) -> Path:
        return self.repo / RAW / "comcat_tectonic"

    @property
    def stations(self) -> Path:
        return self.repo / INTERIM / "stations"

    @property
    def responses(self) -> Path:
        return self.repo / RAW / "responses"

    @property
    def waveforms(self) -> Path:
        return self.repo / RAW / "waveforms"

    @property
    def out(self) -> Path:
        return self.repo / "data" / "features" / "discriminator"

    def ensure(self) -> None:
        for path in (self.tectonic, self.stations, self.responses, self.waveforms, self.out):
            path.mkdir(parents=True, exist_ok=True)


def _client() -> httpx.Client:
    return httpx.Client(timeout=300.0, follow_redirects=True)


def _backoff(attempt: int) -> None:
    """Exponential backoff with jitter. GEOFON answers 503 under concurrency, EarthScope 429."""
    time.sleep(min(30.0, 2.0**attempt) * (0.5 + random.random()))


def _get(client: httpx.Client, url: str, params: dict[str, str], attempts: int = 5) -> bytes | None:
    """GET with backoff; None on a 204/404 (the service has no data), raise on repeated failure."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
            if response.status_code in (204, 404):
                return None
            response.raise_for_status()
            return response.content
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                _backoff(attempt)
    raise BuildError(f"{url} failed after {attempts} attempts: {last}")


def _post(client: httpx.Client, url: str, body: str, attempts: int = 5) -> bytes | None:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.post(url, content=body.encode("ascii"))
            if response.status_code in (204, 404):
                return None
            response.raise_for_status()
            return response.content
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                _backoff(attempt)
    raise BuildError(f"{url} POST failed after {attempts} attempts: {last}")


# --- phase 1: tectonic candidates ---------------------------------------------------------


def fetch_tectonic(
    positive: CatalogEntry, cache: _Cache, client: httpx.Client
) -> tuple[Path, list[CatalogEvent]]:
    """ComCat earthquakes around one positive; cached verbatim so a rebuild refetches nothing."""
    path = cache.tectonic / f"{positive.entry_id.replace('/', '_')}.json"
    start, end = cat.tectonic_search_window(positive)
    params = cat.comcat_circle_params(
        positive.latitude,
        positive.longitude,
        start,
        end,
        radius_km=NEGATIVE_MAX_DISTANCE_KM,
        min_magnitude=NEGATIVE_MIN_MAGNITUDE,
    )
    if not path.exists():
        payload = _get(client, COMCAT_QUERY_URL, params)
        path.write_bytes(payload if payload is not None else b'{"features": []}')
    doc = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for feature in doc.get("features", []):
        properties, geometry = feature.get("properties", {}), feature.get("geometry", {})
        coordinates = geometry.get("coordinates") or [None, None, None]
        if properties.get("mag") is None or coordinates[0] is None:
            continue
        events.append(
            CatalogEvent(
                event_id=str(feature["id"]),
                time_utc=datetime.fromtimestamp(properties["time"] / 1000.0, tz=UTC),
                latitude=float(coordinates[1]),
                longitude=float(coordinates[0]),
                depth_km=(None if coordinates[2] is None else float(coordinates[2])),
                magnitude=float(properties["mag"]),
                mag_type=properties.get("magType"),
                event_type=properties.get("type"),
                title=properties.get("title"),
            )
        )
    return path, events


# --- phase 2: station selection -----------------------------------------------------------


def _parse_station_text(text: str, centre: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) < 17:
            continue
        try:
            rows.append(
                {
                    "net": fields[0],
                    "sta": fields[1],
                    "loc": fields[2],
                    "cha": fields[3],
                    "lat": float(fields[4]),
                    "lon": float(fields[5]),
                    "rate": float(fields[14]) if fields[14] else None,
                    "centre": centre,
                }
            )
        except ValueError:
            continue
    return rows


def fetch_stations(
    positive: CatalogEntry, cache: _Cache, client: httpx.Client
) -> tuple[Path, list[StationChoice]]:
    """Choose the stations for one positive's group; cached as JSON so windows agree."""
    path = cache.stations / f"{positive.event_group}.json"
    if path.exists():
        return path, [
            StationChoice.model_validate(row)
            for row in json.loads(path.read_text(encoding="utf-8"))["stations"]
        ]
    rows: list[dict[str, object]] = []
    centre_errors: dict[str, str] = {}
    for centre, base in DATA_CENTRES.items():
        params = {
            "latitude": f"{positive.latitude:.4f}",
            "longitude": f"{positive.longitude:.4f}",
            "minradius": f"{100 / KM_PER_DEGREE:.4f}",
            "maxradius": f"{1500 / KM_PER_DEGREE:.4f}",
            "channel": "BH?,HH?",
            "level": "channel",
            "starttime": positive.window_start_utc.replace(tzinfo=None).isoformat(
                timespec="seconds"
            ),
            "endtime": positive.window_end_utc.replace(tzinfo=None).isoformat(timespec="seconds"),
            "includerestricted": "false",
            "format": "text",
            "nodata": "204",
        }
        # A data centre being unreachable narrows the station pool for this event; it is
        # recorded in the selection file and does not abort the build, because aborting would
        # make the whole dataset hostage to one archive's uptime. The narrowing stays visible.
        try:
            payload = _get(client, f"{base}/fdsnws/station/1/query", params)
        except BuildError as exc:
            centre_errors[centre] = str(exc)
            continue
        if payload:
            rows.extend(_parse_station_text(payload.decode("utf-8", "replace"), centre))
    stations = select_stations(positive, rows)
    path.write_text(
        json.dumps(
            {
                "event_group": positive.event_group,
                "entry_id": positive.entry_id,
                "n_channels_offered": len(rows),
                "centre_errors": centre_errors,
                "stations": [s.model_dump() for s in stations],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return path, stations


# --- phase 3: responses and waveforms -----------------------------------------------------


def fetch_response(
    station: StationChoice, entry: CatalogEntry, cache: _Cache, client: httpx.Client
) -> Path | None:
    """level=response StationXML for one station, cached per station per year."""
    year = entry.origin_utc.year
    path = cache.responses / f"{station.key.replace('.', '_')}_{year}.xml"
    if path.exists():
        return path if path.stat().st_size > 0 else None
    base = DATA_CENTRES.get(_centre_key(station), DATA_CENTRES["earthscope"])
    params = {
        "network": station.network,
        "station": station.station,
        "location": station.location or "--",
        "channel": f"{station.band_code}H?",
        "level": "response",
        "starttime": f"{year}-01-01T00:00:00",
        "endtime": f"{year + 1}-01-01T00:00:00",
        "format": "xml",
        "nodata": "204",
    }
    try:
        payload = _get(client, f"{base}/fdsnws/station/1/query", params)
    except BuildError:
        return None  # not cached empty: a transient outage must not poison a resumed build
    path.write_bytes(payload or b"")
    return path if payload else None


def _centre_key(station: StationChoice) -> str:
    for key, base in DATA_CENTRES.items():
        if station.data_centre == base:
            return key
    return station.data_centre or "earthscope"


def fetch_waveforms(
    entry: CatalogEntry,
    stations: Sequence[StationChoice],
    cache: _Cache,
    client: httpx.Client,
) -> Path | None:
    """One bulk dataselect request for a whole window; cached as a single MiniSEED file."""
    path = cache.waveforms / f"{entry.entry_id.replace('/', '_')}.mseed"
    marker = path.with_suffix(".empty")
    if path.exists():
        return path
    if marker.exists():
        return None
    by_centre: dict[str, list[StationChoice]] = {}
    for station in stations:
        by_centre.setdefault(_centre_key(station), []).append(station)
    payloads: list[bytes] = []
    for centre, group in by_centre.items():
        base = DATA_CENTRES.get(centre, DATA_CENTRES["earthscope"])
        body = "\n".join(" ".join(row) for row in bulk_rows_for(group, entry)) + "\n"
        try:
            payload = _post(client, f"{base}/fdsnws/dataselect/1/query", body)
        except BuildError:
            continue  # the other centre may still deliver; a total miss is marked below
        if payload:
            payloads.append(payload)
    if not payloads:
        marker.write_text("no data returned by dataselect\n", encoding="utf-8")
        return None
    path.write_bytes(b"".join(payloads))
    return path


# --- planning -----------------------------------------------------------------------------


def _cross_tab(entries: Sequence[CatalogEntry], key: str) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for entry in entries:
        row = table.setdefault(str(getattr(entry, key)), {})
        for label in ClassLabel:
            row.setdefault(label.value, 0)
        row[entry.class_label.value] += 1
    return {outer: dict(sorted(inner.items())) for outer, inner in sorted(table.items())}


def plan_build(
    catalogue: DiscriminatorCatalog,
    stations_by_group: dict[str, list[StationChoice]],
) -> BuildPlan:
    """Counts and a byte estimate with its basis. Writes nothing."""
    waveform_bytes = 0
    unique: set[str] = set()
    warnings_: list[str] = []
    for entry in catalogue.entries:
        stations = stations_by_group.get(entry.event_group, [])
        if not stations:
            warnings_.append(f"{entry.entry_id}: no stations selected")
            continue
        waveform_bytes += estimate_window_bytes(stations)
        unique.update(s.key for s in stations)
    response_bytes = len(unique) * 150_000
    zarr_bytes = int(
        len(catalogue.entries)
        * MAX_STATIONS_PER_EVENT
        * len(COMPONENTS)
        * N_SAMPLES
        * 4
        * 0.55  # zstd on band-limited float32 seismic, measured at roughly 45% saving
    )
    positives = catalogue.by_class(ClassLabel.mass_movement)
    return BuildPlan(
        n_positives=len(positives),
        n_negatives=len(catalogue.by_class(ClassLabel.tectonic)),
        n_noise=len(catalogue.by_class(ClassLabel.noise)),
        n_windows=len(catalogue.entries),
        n_groups=len(catalogue.groups),
        n_unique_stations=len(unique),
        estimated_waveform_bytes=waveform_bytes,
        estimated_response_bytes=response_bytes,
        estimated_zarr_bytes=zarr_bytes,
        estimated_total_bytes=waveform_bytes + response_bytes + zarr_bytes,
        estimate_basis=(
            f"waveforms: {ESTIMATE_BASIS}. responses: 150 kB per unique station-year "
            "(level=response StationXML, measured on the committed fixtures). zarr: "
            f"n_windows x {MAX_STATIONS_PER_EVENT} x {len(COMPONENTS)} x {N_SAMPLES} x 4 B "
            "float32 x 0.55 for zstd."
        ),
        class_by_region=_cross_tab(catalogue.entries, "region_id"),
        class_by_decade=_cross_tab(catalogue.entries, "decade"),
        warnings=warnings_[:20],
    )


# --- ledger -------------------------------------------------------------------------------


def _entry_for_file(
    path: Path,
    repo: Path,
    *,
    source: DataSource,
    product_id: str,
    event_id: str,
    url: str,
    params: dict[str, Any],
    licence: str,
    licence_url: str,
    notes: str,
) -> ManifestEntry:
    return ManifestEntry(
        source=source,
        product_id=product_id,
        event_id=event_id,
        path=path.resolve().relative_to(repo.resolve()).as_posix(),
        url=url,
        params=params,
        sha256=sha256_of_file(path),
        size_bytes=path.stat().st_size,
        retrieved_at=datetime.now(tz=UTC),
        licence=licence,
        licence_source_url=licence_url,
        provenance=Provenance.real,
        status=ManifestStatus.fetched,
        adapter=ADAPTER_NAME,
        adapter_version=BUILD_VERSION,
        notes=notes,
    )


def _not_fetched_entry(entry: CatalogEntry, reason: str) -> ManifestEntry:
    """The honest record of an event serac wanted and could not have."""
    return ManifestEntry(
        source=DataSource.fdsn_waveforms,
        product_id=f"discriminator/{entry.entry_id}",
        event_id=entry.event_group,
        params={
            "class_label": entry.class_label.value,
            "origin_utc": entry.origin_utc.isoformat(),
            "source_ids": entry.source_ids,
        },
        licence=LICENCE_NULL,
        licence_source_url=FDSN_TERMS_URL,
        provenance=Provenance.real,
        status=ManifestStatus.not_fetched,
        time_start=entry.window_start_utc,
        time_end=entry.window_end_utc,
        adapter=ADAPTER_NAME,
        adapter_version=BUILD_VERSION,
        notes=(
            f"excluded from the discriminator set: {reason}. Not substituted, not backfilled "
            "and not replaced by another event."
        ),
    )


# --- phase 4: processing ------------------------------------------------------------------


@dataclass
class _Processed:
    waveform: np.ndarray
    valid: np.ndarray
    reason: str | None = None
    stations: list[StationChoice] = field(default_factory=list)


def process_window(
    entry: CatalogEntry,
    stations: Sequence[StationChoice],
    mseed_path: Path | None,
    response_paths: dict[str, Path | None],
) -> _Processed:
    """Read one window's MiniSEED, remove responses, and lay it out on the fixed station axis."""
    from obspy import read, read_inventory

    empty = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32)
    mask = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
    if mseed_path is None or not mseed_path.exists():
        return _Processed(empty, mask, "dataselect returned no data for any selected station")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            stream = read(str(mseed_path), format="MSEED")
        except Exception as exc:
            return _Processed(empty, mask, f"MiniSEED unreadable: {exc}")

        kept: list[StationChoice] = []
        for station in stations[:MAX_STATIONS_PER_EVENT]:
            response_path = response_paths.get(station.key)
            if response_path is None or not response_path.exists():
                continue
            picked = stream.select(
                network=station.network, station=station.station, channel=f"{station.band_code}H?"
            )
            if station.location:
                picked = picked.select(location=station.location)
            if len(picked) == 0:
                continue
            try:
                inventory = read_inventory(str(response_path), format="STATIONXML")
            except Exception:
                continue
            try:
                block, valid = process_station_window(picked, inventory, entry, station)
            except MissingResponseError:
                continue
            except Exception:
                continue
            if not valid.any():
                continue
            slot = len(kept)
            empty[slot] = block
            mask[slot] = valid
            kept.append(station)
            if slot + 1 >= MAX_STATIONS_PER_EVENT:
                break

    if len(kept) < MIN_STATIONS_PER_WINDOW:
        return _Processed(
            empty,
            mask,
            f"only {len(kept)} station(s) yielded response-removed data; "
            f"{MIN_STATIONS_PER_WINDOW} are required",
            kept,
        )
    return _Processed(empty, mask, None, kept)


# --- the build ----------------------------------------------------------------------------


def build_dataset(
    repo: Path,
    catalogue: DiscriminatorCatalog,
    stations_by_group: dict[str, list[StationChoice]],
    ledger: ManifestLedger,
    *,
    workers: int = 8,
    progress: Any = None,
) -> BuildReport:
    """Fetch every window, process it, write the Zarr store and the chunk-hash index."""
    cache = _Cache(repo)
    cache.ensure()
    report = BuildReport(built_at_utc=datetime.now(tz=UTC))
    entries = list(catalogue.entries)
    report.n_windows_requested = len(entries)
    report.positives_requested = len(catalogue.by_class(ClassLabel.mass_movement))

    # Responses first: they are shared across every window of a group.
    with _client() as client:
        needed: dict[str, tuple[StationChoice, CatalogEntry]] = {}
        for entry in entries:
            for station in stations_by_group.get(entry.event_group, []):
                needed.setdefault(f"{station.key}_{entry.origin_utc.year}", (station, entry))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(
                pool.map(
                    lambda pair: fetch_response(pair[0], pair[1], cache, client), needed.values()
                )
            )

        # Waveforms.
        def _fetch(entry: CatalogEntry) -> tuple[str, Path | None]:
            stations = stations_by_group.get(entry.event_group, [])
            if not stations:
                return entry.entry_id, None
            try:
                return entry.entry_id, fetch_waveforms(entry, stations, cache, client)
            except BuildError:
                return entry.entry_id, None

        paths: dict[str, Path | None] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, (entry_id, path) in enumerate(pool.map(_fetch, entries), 1):
                paths[entry_id] = path
                if progress is not None and index % 25 == 0:
                    progress(f"fetched {index}/{len(entries)} windows")

    # Processing and writing.
    store = open_store(cache.out, n_windows=len(entries), mode="w")
    records = []
    slot = 0
    for index, entry in enumerate(entries):
        stations = stations_by_group.get(entry.event_group, [])
        response_paths = {
            s.key: (
                cache.responses / f"{s.key.replace('.', '_')}_{entry.origin_utc.year}.xml"
                if (
                    cache.responses / f"{s.key.replace('.', '_')}_{entry.origin_utc.year}.xml"
                ).exists()
                else None
            )
            for s in stations
        }
        processed = process_window(entry, stations, paths.get(entry.entry_id), response_paths)
        if processed.reason is not None:
            ledger.append(_not_fetched_entry(entry, processed.reason))
            report.not_fetched[entry.entry_id] = processed.reason
            report.n_windows_not_fetched += 1
            continue
        write_window(store, slot, processed.waveform, processed.valid)
        records.append(make_record(slot, entry, processed.stations, processed.valid))
        slot += 1
        if entry.class_label is ClassLabel.mass_movement:
            report.positives_written += 1
        if progress is not None and index % 50 == 0:
            progress(f"processed {index}/{len(entries)} windows, {slot} written")

    report.n_windows_written = slot
    index_model = DatasetIndex(
        built_at_utc=report.built_at_utc,
        n_windows=slot,
        sampling_rate_hz=TARGET_SAMPLING_RATE_HZ,
        bandpass_hz=BANDPASS_HZ,
        windows=records,
        notes=list(catalogue.notes),
    )
    write_index(cache.out, index_model)
    _, chunk_sha, n_files = write_chunk_index(cache.out)
    report.chunk_index_sha256 = chunk_sha
    report.n_chunk_files = n_files

    for path in sorted(cache.waveforms.glob("*.mseed")):
        report.bytes_fetched += path.stat().st_size
    for path in sorted(cache.responses.glob("*.xml")):
        report.bytes_fetched += path.stat().st_size

    ledger.append(
        ManifestEntry(
            source=DataSource.fdsn_waveforms,
            product_id="discriminator/windows.zarr",
            product_level="discriminator-window-store",
            path=(cache.out / "chunk_hashes.tsv").resolve().relative_to(repo.resolve()).as_posix(),
            params={
                "n_windows": slot,
                "n_chunk_files": n_files,
                "sampling_rate_hz": TARGET_SAMPLING_RATE_HZ,
                "bandpass_hz": list(BANDPASS_HZ),
                "response_removed_to": "velocity",
            },
            sha256=chunk_sha,
            size_bytes=(cache.out / "chunk_hashes.tsv").stat().st_size,
            retrieved_at=report.built_at_utc,
            licence=LICENCE_NULL,
            licence_source_url=FDSN_TERMS_URL,
            provenance=Provenance.derived,
            status=ManifestStatus.fetched,
            adapter=ADAPTER_NAME,
            adapter_version=BUILD_VERSION,
            notes=(
                "Sorted chunk-hash index of the discriminator Zarr store; its sha256 pins every "
                f"byte of the {n_files}-file store. Derived from real FDSN waveforms by response "
                "removal to velocity, 0.005-5 Hz bandpass and resampling to 20 Hz. " + FDSN_NOTE
            ),
        )
    )
    return report
