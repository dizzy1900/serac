"""FDSN web-services archive adapter (`WaveformArchive`) over the ObsPy fdsnws client.

The client is injected (tests pass a fake) or built by alias name on first use. Whatever the
alias, every record this adapter produces carries the client's **resolved** `base_url`
(`https://service.earthscope.org`, never `IRIS`), so a future re-mapping of an alias cannot
silently change provenance (ADR-0015).

`plan()` is the dry run: it lists the fdsnws bulk rows and estimates the download size from a
stated per-sample assumption; it never writes anything, not even a ledger line. `fetch()`
stores the bytes exactly as returned by dataselect (no re-encoding), the channel-level
StationXML, a sidecar `manifest.json` (`FixtureManifest`), and one ledger row per file.

Licence handling matches the phase-1 fixtures: the data centres consulted publish terms of
service rather than a licence, so `licence` is recorded as `null: see licence_source_url` and
the terms URL is stored alongside; attribution requirements go in `notes`.
"""

from __future__ import annotations

import io
import math
import warnings
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import obspy
from obspy import Inventory, Stream, UTCDateTime, read, read_inventory

from serac.adapters.storage.manifest_ledger import sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.domain.replay import FixtureFile, FixtureManifest, FixtureRequest, TimeWindow
from serac.domain.seismic import Sncl
from serac.errors import SeracError
from serac.ports.ledger import ManifestLedger
from serac.ports.seismic import (
    FetchPlan,
    FetchResult,
    StationQuery,
    StationRef,
    WaveformArchive,
    WaveformRequest,
)

ADAPTER_NAME = "FdsnWaveformArchive"
ADAPTER_VERSION = "0.1.0"

DEFAULT_CLIENT = "EARTHSCOPE"
EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = 2 * math.pi * EARTH_RADIUS_KM / 360

# Steim2 compresses broadband counts to roughly one to two bytes per sample; the upper end is
# used so the estimate errs high. Stated in every plan's `estimate_basis`.
ASSUMED_BYTES_PER_SAMPLE = 2.0
# Used only when channel metadata is unavailable to the planner.
ASSUMED_SAMPLING_RATE_HZ = 50.0
STATIONXML_ESTIMATE_BYTES = 5_000

LICENCE_NULL = "null: see licence_source_url"

# Terms-of-service pages for the data centres serac has consulted, keyed by resolved base URL.
# EarthScope's page was read in the phase-1 fixture session; any other base URL falls back to
# the base URL itself and the ledger notes say the terms were not read.
TERMS_BY_BASE_URL: dict[str, tuple[str, str]] = {
    "https://service.earthscope.org": (
        "https://www.earthscope.org/terms-of-service/",
        "EarthScope Terms of Service state no licence; users must acknowledge EarthScope "
        "Consortium (NSF award 2435260) and cite network DOIs.",
    ),
}


class FdsnAdapterError(SeracError):
    """The FDSN adapter could not complete a request."""


class FdsnClientLike(Protocol):
    """The subset of `obspy.clients.fdsn.Client` the adapter uses."""

    base_url: str

    def get_stations(self, **kwargs: Any) -> Inventory: ...

    def get_waveforms_bulk(self, bulk: Any, **kwargs: Any) -> Any: ...


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a spherical Earth (mean radius); adequate for station lists."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _fdsn_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _to_utc(value: UTCDateTime) -> datetime:
    return datetime.fromtimestamp(float(value.timestamp), tz=UTC)


def _location_for_bulk(location: str) -> str:
    return location or "--"


def bulk_rows(sncls: Iterable[Sncl], start: datetime, end: datetime) -> list[list[str]]:
    """fdsnws bulk rows `net sta loc cha start end`, empty location written as `--`."""
    return [
        [
            s.network,
            s.station,
            _location_for_bulk(s.location),
            s.channel,
            _fdsn_time(start),
            _fdsn_time(end),
        ]
        for s in sncls
    ]


def stations_from_inventory(
    inventory: Inventory,
    *,
    data_centre: str,
    origin: tuple[float, float] | None = None,
) -> list[StationRef]:
    """Flatten an ObsPy inventory (channel level) into `StationRef`s, one per channel."""
    out: list[StationRef] = []
    for network in inventory:
        for station in network:
            for channel in station:
                distance = None
                if origin is not None:
                    distance = haversine_km(
                        origin[0], origin[1], float(channel.latitude), float(channel.longitude)
                    )
                out.append(
                    StationRef(
                        sncl=Sncl(
                            network=str(network.code),
                            station=str(station.code),
                            location=str(channel.location_code or ""),
                            channel=str(channel.code),
                        ),
                        latitude=float(channel.latitude),
                        longitude=float(channel.longitude),
                        elevation_m=(
                            float(channel.elevation) if channel.elevation is not None else None
                        ),
                        sampling_rate_hz=(
                            float(channel.sample_rate) if channel.sample_rate else None
                        ),
                        distance_km=distance,
                        data_centre=data_centre,
                        restricted=(
                            None
                            if network.restricted_status is None
                            else str(network.restricted_status) != "open"
                        ),
                    )
                )
    out.sort(key=lambda s: (s.distance_km if s.distance_km is not None else math.inf, s.sncl.key))
    return out


class FdsnWaveformArchive(WaveformArchive):
    """`WaveformArchive` over fdsnws-station and fdsnws-dataselect."""

    def __init__(
        self,
        client: FdsnClientLike | None = None,
        *,
        client_name: str = DEFAULT_CLIENT,
        timeout: float = 60.0,
        repo_root: Path | None = None,
        lookup_metadata_for_plan: bool = True,
    ) -> None:
        self._client = client
        self.client_name = client_name
        self.timeout = timeout
        self.repo_root = repo_root
        self.lookup_metadata_for_plan = lookup_metadata_for_plan

    # --- client -------------------------------------------------------------------------

    @property
    def client(self) -> FdsnClientLike:
        """The injected client, or an ObsPy client built by alias on first use (network)."""
        if self._client is None:
            from obspy.clients.fdsn import Client

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._client = Client(self.client_name, timeout=self.timeout)
        return self._client

    @property
    def base_url(self) -> str:
        """Resolved service base URL, never the alias."""
        url = str(self.client.base_url).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise FdsnAdapterError(f"client base_url {url!r} is not a resolved URL")
        return url

    def terms(self) -> tuple[str, str]:
        """(licence_source_url, note) for the resolved data centre."""
        base = self.base_url
        if base in TERMS_BY_BASE_URL:
            return TERMS_BY_BASE_URL[base]
        return base, "terms of service for this data centre were not read in-session"

    # --- station search -----------------------------------------------------------------

    def search_stations(self, query: StationQuery) -> list[StationRef]:
        kwargs: dict[str, Any] = {
            "latitude": query.latitude,
            "longitude": query.longitude,
            "maxradius": query.max_radius_km / KM_PER_DEGREE,
            "minradius": query.min_radius_km / KM_PER_DEGREE,
            "channel": ",".join(query.channels),
            "level": "channel",
            "starttime": UTCDateTime(query.start_utc.astimezone(UTC).timestamp()),
            "endtime": UTCDateTime(query.end_utc.astimezone(UTC).timestamp()),
            "includerestricted": query.include_restricted,
        }
        if query.networks:
            kwargs["network"] = ",".join(query.networks)
        try:
            inventory = self.client.get_stations(**kwargs)
        except Exception as exc:  # obspy raises FDSNException subclasses and socket errors
            raise FdsnAdapterError(
                f"fdsnws-station query failed at {self.base_url}: {exc}"
            ) from exc
        return stations_from_inventory(
            inventory, data_centre=self.base_url, origin=(query.latitude, query.longitude)
        )

    def find_stations(self, query: StationQuery) -> list[StationRef]:
        """Alias of `search_stations` (the brief's name)."""
        return self.search_stations(query)

    # --- dry run ------------------------------------------------------------------------

    def _channel_rates(self, request: WaveformRequest) -> dict[str, float]:
        """Sampling rates by SNCL key from channel metadata; empty when unavailable."""
        if not self.lookup_metadata_for_plan:
            return {}
        inventory = self._station_inventory(request)
        if inventory is None:
            return {}
        return {
            ref.sncl.key: ref.sampling_rate_hz
            for ref in stations_from_inventory(inventory, data_centre=self.base_url)
            if ref.sampling_rate_hz
        }

    def _station_inventory(self, request: WaveformRequest) -> Inventory | None:
        kwargs = {
            "network": ",".join(sorted({s.network for s in request.sncls})),
            "station": ",".join(sorted({s.station for s in request.sncls})),
            "channel": ",".join(sorted({s.channel for s in request.sncls})),
            "level": "channel",
            "starttime": UTCDateTime(request.start_utc.astimezone(UTC).timestamp()),
            "endtime": UTCDateTime(request.end_utc.astimezone(UTC).timestamp()),
        }
        try:
            return self.client.get_stations(**kwargs)
        except Exception:
            return None

    def plan(self, request: WaveformRequest) -> FetchPlan:
        rates = self._channel_rates(request)
        duration_s = (request.end_utc - request.start_utc).total_seconds()
        warnings_: list[str] = []
        total = 0.0
        for sncl in request.sncls:
            rate = rates.get(sncl.key)
            if rate is None:
                rate = ASSUMED_SAMPLING_RATE_HZ
                warnings_.append(
                    f"{sncl.key}: no channel metadata; assumed {ASSUMED_SAMPLING_RATE_HZ} Hz"
                )
            total += duration_s * rate * ASSUMED_BYTES_PER_SAMPLE
        if request.with_stations:
            total += STATIONXML_ESTIMATE_BYTES
        basis = (
            f"duration x sampling rate x {ASSUMED_BYTES_PER_SAMPLE} bytes/sample (Steim2 upper "
            f"bound); rates from channel metadata where available, else "
            f"{ASSUMED_SAMPLING_RATE_HZ} Hz; StationXML assumed {STATIONXML_ESTIMATE_BYTES} B"
        )
        if duration_s > 24 * 3600:
            warnings_.append("window longer than 24 h; consider narrower event windows")
        return FetchPlan(
            request=request,
            data_centre=self.base_url,
            bulk=bulk_rows(request.sncls, request.start_utc, request.end_utc),
            estimated_bytes=math.ceil(total),
            estimate_basis=basis,
            warnings=warnings_,
        )

    # --- fetch --------------------------------------------------------------------------

    def _dataselect_url(self, row: list[str]) -> str:
        net, sta, loc, cha, start, end = row
        return (
            f"{self.base_url}/fdsnws/dataselect/1/query?net={net}&sta={sta}&loc={loc}"
            f"&cha={cha}&start={start}&end={end}"
        )

    def _station_url(self, request: WaveformRequest) -> str:
        nets = ",".join(sorted({s.network for s in request.sncls}))
        stas = ",".join(sorted({s.station for s in request.sncls}))
        chas = ",".join(sorted({s.channel for s in request.sncls}))
        return (
            f"{self.base_url}/fdsnws/station/1/query?net={nets}&sta={stas}&cha={chas}"
            f"&level=channel&start={_fdsn_time(request.start_utc)}"
            f"&end={_fdsn_time(request.end_utc)}"
        )

    def _rel(self, path: Path) -> str:
        if self.repo_root is not None:
            try:
                return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    def _fetch_channel(self, row: list[str]) -> bytes | None:
        """Raw dataselect bytes for one bulk row, or None when the service has no data."""
        buffer = io.BytesIO()
        try:
            self.client.get_waveforms_bulk([row], filename=buffer)
        except Exception as exc:
            text = str(exc)
            if "No data" in text or "204" in text:
                return None
            raise FdsnAdapterError(f"dataselect failed for {row[:4]}: {exc}") from exc
        payload = buffer.getvalue()
        return payload or None

    def _fetch_stationxml(self, request: WaveformRequest) -> bytes | None:
        buffer = io.BytesIO()
        kwargs = {
            "network": ",".join(sorted({s.network for s in request.sncls})),
            "station": ",".join(sorted({s.station for s in request.sncls})),
            "channel": ",".join(sorted({s.channel for s in request.sncls})),
            "level": "channel",
            "starttime": UTCDateTime(request.start_utc.astimezone(UTC).timestamp()),
            "endtime": UTCDateTime(request.end_utc.astimezone(UTC).timestamp()),
            "filename": buffer,
        }
        try:
            self.client.get_stations(**kwargs)
        except Exception as exc:
            raise FdsnAdapterError(f"fdsnws-station failed: {exc}") from exc
        return buffer.getvalue() or None

    def fetch(self, plan: FetchPlan, dest_dir: Path, ledger: ManifestLedger) -> FetchResult:
        if plan.refusals:
            raise FdsnAdapterError(f"plan carries refusals: {plan.refusals}")
        request = plan.request
        dest_dir.mkdir(parents=True, exist_ok=True)
        retrieved_at = datetime.now(tz=UTC)
        licence_url, licence_note = self.terms()
        files: list[FixtureFile] = []
        entries: list[ManifestEntry] = []
        written: list[str] = []
        missing: list[str] = []

        for row, sncl in zip(plan.bulk, request.sncls, strict=True):
            payload = self._fetch_channel(row)
            if payload is None:
                missing.append(sncl.key)
                continue
            stream: Stream = read(io.BytesIO(payload), format="MSEED")
            if len(stream) == 0:
                missing.append(sncl.key)
                continue
            path = dest_dir / f"{sncl.key}.mseed"
            path.write_bytes(payload)
            first = stream[0]
            file = FixtureFile(
                path=path.name,
                kind="miniseed",
                sha256=sha256_of_file(path),
                size_bytes=path.stat().st_size,
                sncl=sncl.key,
                start_utc=_to_utc(min(tr.stats.starttime for tr in stream)),
                end_utc=_to_utc(max(tr.stats.endtime for tr in stream)),
                sampling_rate_hz=float(first.stats.sampling_rate),
                npts=int(sum(tr.stats.npts for tr in stream)),
                url=self._dataselect_url(row),
            )
            files.append(file)
            written.append(self._rel(path))
            entries.append(
                self._entry(
                    file, path, request, retrieved_at, licence_url, licence_note, product="miniseed"
                )
            )

        if request.with_stations and files:
            xml = self._fetch_stationxml(request)
            if xml is not None:
                path = dest_dir / "stations.xml"
                path.write_bytes(xml)
                file = FixtureFile(
                    path=path.name,
                    kind="stationxml",
                    sha256=sha256_of_file(path),
                    size_bytes=path.stat().st_size,
                    url=self._station_url(request),
                )
                files.append(file)
                written.append(self._rel(path))
                entries.append(
                    self._entry(
                        file,
                        path,
                        request,
                        retrieved_at,
                        licence_url,
                        licence_note,
                        product="stationxml",
                    )
                )

        status: Literal["fetched", "partial", "not_fetched"]
        if not files:
            status = "not_fetched"
        elif missing:
            status = "partial"
        else:
            status = "fetched"

        manifest = FixtureManifest(
            event_id=request.event_id,
            window=TimeWindow(start_utc=request.start_utc, end_utc=request.end_utc),
            files=files,
            missing=missing,
            request=FixtureRequest(
                client=self.client_name,
                base_url=self.base_url,
                bulk=plan.bulk,
                station_level="channel" if request.with_stations else None,
                tool=f"obspy {obspy.__version__}",
            ),
            retrieved_at_utc=retrieved_at if files else None,
            licence=None,
            licence_source_url=licence_url,
            status=status,
            notes=(
                f"{licence_note} Bytes are exactly as returned by fdsnws-dataselect; no "
                "re-encoding. Origin time is not stored here: replay reads it from the "
                "event-library record."
            ),
        )
        manifest_path = dest_dir / "manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written.append(self._rel(manifest_path))

        for entry in entries:
            ledger.append(entry)
        if not files:
            ledger.append(
                ManifestEntry(
                    source=DataSource.fdsn_waveforms,
                    product_id=f"{request.event_id}/waveforms",
                    event_id=request.event_id,
                    url=self.base_url,
                    params={"bulk": plan.bulk},
                    licence=LICENCE_NULL,
                    licence_source_url=licence_url,
                    provenance=Provenance.real,
                    status=ManifestStatus.not_fetched,
                    time_start=request.start_utc,
                    time_end=request.end_utc,
                    adapter=ADAPTER_NAME,
                    adapter_version=ADAPTER_VERSION,
                    notes="dataselect returned no data for any requested channel",
                )
            )
        return FetchResult(
            plan=plan,
            dest_dir=self._rel(dest_dir),
            files=written,
            missing=missing,
            entries=entries,
            status=manifest.status,
        )

    def _entry(
        self,
        file: FixtureFile,
        path: Path,
        request: WaveformRequest,
        retrieved_at: datetime,
        licence_url: str,
        licence_note: str,
        *,
        product: str,
    ) -> ManifestEntry:
        return ManifestEntry(
            source=DataSource.fdsn_waveforms,
            product_id=f"{request.event_id}/{file.path}",
            product_level=product,
            event_id=request.event_id,
            path=self._rel(path),
            url=file.url,
            params={"base_url": self.base_url, "client": self.client_name},
            sha256=file.sha256,
            size_bytes=file.size_bytes,
            retrieved_at=retrieved_at,
            licence=LICENCE_NULL,
            licence_source_url=licence_url,
            provenance=Provenance.real,
            status=ManifestStatus.fetched,
            time_start=file.start_utc or request.start_utc,
            time_end=file.end_utc or request.end_utc,
            adapter=ADAPTER_NAME,
            adapter_version=ADAPTER_VERSION,
            notes=licence_note,
        )


def load_inventory(path: Path) -> Inventory:
    """Read a StationXML file (helper for tests and replay station listings)."""
    return read_inventory(str(path), format="STATIONXML")
