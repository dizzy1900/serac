"""Fetch the LH? waveform fixtures the force-history gate runs on offline.

1 sps LH? channels are enough because the inversion works at 20-150 s, which is what keeps
these fixtures small enough to commit. Stations are chosen for azimuthal spread rather than
proximity: the whole point of the geometry check is that coverage, not station count, decides
whether a location can be published, so a fixture set that is dense on one side and empty on
the other would quietly defeat it.

Only open (unrestricted) channels are requested. Every file is hashed and ledgered.

    uv run python scripts/fetch_lfh_fixtures.py --target bingham-canyon-2013-1
    uv run python scripts/fetch_lfh_fixtures.py            # all targets
"""

# ruff: noqa: T201  (a script; progress goes to stdout)
from __future__ import annotations

import argparse
import gzip
import io
import json
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from serac.adapters.seismic.syngine import geocentric_distance_azimuth
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance

warnings.simplefilter("ignore")

ADAPTER = "fetch_lfh_fixtures"
ADAPTER_VERSION = "0.1.0"
LICENCE = "null: see licence_source_url"
LICENCE_URL = "https://www.earthscope.org/terms-of-service/"
LICENCE_NOTE = (
    "Data centres publish terms of service rather than a licence. Acknowledge the operating "
    "networks and cite their DOIs; EarthScope requests acknowledgement of NSF award 2435260."
)

DATA_CENTRES = ("EARTHSCOPE", "GEOFON", "ORFEUS", "RESIF", "INGV")


@dataclass(frozen=True)
class FetchTarget:
    target_id: str
    latitude: float
    longitude: float
    origin_utc: str
    max_stations: int = 10


def load_targets(repo: Path) -> list[FetchTarget]:
    """Read the targets from `data/references/lfh_published.json`, never from a copy here.

    Origin times and source positions live in exactly one place. They were duplicated once,
    and the copy went stale: the Blatten origin here stayed at the press time of "gegen 15.30
    Uhr" after the reference file had been updated to the Swiss Seismological Service
    catalogue entry five and a half minutes earlier, so the fixtures were fetched around a
    window the signal was not in.
    """
    from serac.models.lfh.references import load_references

    return [
        FetchTarget(
            target_id=target.target_id,
            latitude=target.source_latitude,
            longitude=target.source_longitude,
            origin_utc=target.origin_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        for target in load_references(repo).targets
    ]


WINDOW_BEFORE_S = 180.0
WINDOW_AFTER_S = 840.0
MIN_DISTANCE_DEG = 0.5
MAX_DISTANCE_DEG = 15.0


def _azimuthal_gap(azimuths: list[float]) -> float:
    if len(azimuths) < 2:
        return 360.0
    ordered = sorted(a % 360.0 for a in azimuths)
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    gaps.append(ordered[0] + 360.0 - ordered[-1])
    return max(gaps)


def discover(target: FetchTarget) -> list[dict[str, object]]:
    """Open LH? channels in the distance window, one entry per station."""
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    origin = UTCDateTime(target.origin_utc)
    found: dict[tuple[str, str], dict[str, object]] = {}
    for centre in DATA_CENTRES:
        try:
            client = Client(centre, timeout=120)
            inventory = client.get_stations(
                latitude=target.latitude,
                longitude=target.longitude,
                minradius=MIN_DISTANCE_DEG,
                maxradius=MAX_DISTANCE_DEG,
                channel="LH?",
                level="channel",
                starttime=origin - WINDOW_BEFORE_S,
                endtime=origin + WINDOW_AFTER_S,
                includerestricted=False,
            )
        except Exception as exc:
            print(f"  [{centre}] {type(exc).__name__}: {str(exc)[:70]}")
            continue
        for network in inventory:
            for station in network:
                channels = sorted({str(c.code) for c in station})
                if not channels:
                    continue
                first = station[0]
                distance, azimuth = geocentric_distance_azimuth(
                    target.latitude,
                    target.longitude,
                    float(first.latitude),
                    float(first.longitude),
                )
                key = (str(network.code), str(station.code))
                record = {
                    "network": str(network.code),
                    "station": str(station.code),
                    "location": str(first.location_code or ""),
                    "channels": channels,
                    "latitude": float(first.latitude),
                    "longitude": float(first.longitude),
                    "distance_deg": distance,
                    "azimuth_deg": azimuth,
                    "data_centre": centre,
                }
                if key not in found or len(channels) > len(found[key]["channels"]):  # type: ignore[arg-type]
                    found[key] = record
    return sorted(found.values(), key=lambda r: float(r["distance_deg"]))  # type: ignore[arg-type]


def choose(candidates: list[dict[str, object]], max_stations: int) -> list[dict[str, object]]:
    """One station per azimuth bin, nearest first, before any bin is revisited.

    Three-component stations are preferred inside a bin: the horizontal components are what
    resolve the force azimuth, and a fixture set of vertical-only channels would make the
    inversion look better conditioned than it is.
    """
    bins: dict[int, list[dict[str, object]]] = {}
    width = 360.0 / max_stations
    for record in candidates:
        index = int(float(record["azimuth_deg"]) % 360.0 // width)  # type: ignore[arg-type]
        bins.setdefault(index, []).append(record)
    for members in bins.values():
        members.sort(key=lambda r: (-len(r["channels"]), float(r["distance_deg"])))  # type: ignore[arg-type,index]
    chosen: list[dict[str, object]] = []
    round_index = 0
    while len(chosen) < max_stations:
        added = False
        for index in sorted(bins):
            members = bins[index]
            if round_index < len(members) and len(chosen) < max_stations:
                chosen.append(members[round_index])
                added = True
        if not added:
            break
        round_index += 1
    return chosen


def fetch(target: FetchTarget, repo: Path, ledger: JsonlManifestLedger) -> dict[str, object]:
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    print(f"=== {target.target_id}")
    candidates = discover(target)
    print(f"  {len(candidates)} open LH? stations in {MIN_DISTANCE_DEG}-{MAX_DISTANCE_DEG} deg")
    chosen = choose(candidates, target.max_stations)
    gap = _azimuthal_gap([float(r["azimuth_deg"]) for r in chosen])  # type: ignore[arg-type]
    print(f"  chose {len(chosen)} stations, azimuthal gap {gap:.0f} deg")

    origin = UTCDateTime(target.origin_utc)
    start, end = origin - WINDOW_BEFORE_S, origin + WINDOW_AFTER_S
    out = repo / "data" / "fixtures" / "lfh" / target.target_id
    out.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(tz=UTC)

    files: list[dict[str, object]] = []
    inventories = []
    for record in chosen:
        centre = str(record["data_centre"])
        client = Client(centre, timeout=180)
        net, sta = str(record["network"]), str(record["station"])
        try:
            # Always wildcard the location code. The code advertised by fdsnws-station for a
            # station's first channel is not always the one the archive holds data under, and
            # a station silently dropped for that reason would quietly worsen the geometry.
            stream = client.get_waveforms(
                network=net,
                station=sta,
                location="*",
                channel="LH?",
                starttime=start,
                endtime=end,
            )
        except Exception as exc:
            print(f"    {net}.{sta}: no waveforms ({type(exc).__name__})")
            continue
        stream.merge(method=1, fill_value=0)
        if len(stream) == 0:
            continue
        try:
            inventory = client.get_stations(
                network=net,
                station=sta,
                location="*",
                channel="LH?",
                level="response",
                starttime=start,
                endtime=end,
            )
        except Exception as exc:
            print(f"    {net}.{sta}: no response metadata ({type(exc).__name__}); skipped")
            continue
        inventories.append(inventory)
        for trace in stream:
            key = (
                f"{trace.stats.network}.{trace.stats.station}."
                f"{trace.stats.location}.{trace.stats.channel}"
            )
            buffer = io.BytesIO()
            trace.write(buffer, format="MSEED", encoding="STEIM2", reclen=512)
            path = out / f"{key}.mseed"
            path.write_bytes(buffer.getvalue())
            digest = sha256_of_file(path)
            files.append(
                {
                    "path": path.name,
                    "sncl": key,
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                    "npts": int(trace.stats.npts),
                    "sampling_rate_hz": float(trace.stats.sampling_rate),
                    "data_centre": centre,
                    "distance_deg": record["distance_deg"],
                    "azimuth_deg": record["azimuth_deg"],
                }
            )
            ledger.append(
                ManifestEntry(
                    source=DataSource.fdsn_waveforms,
                    product_id=f"lfh/{target.target_id}/{path.name}",
                    product_level="miniseed",
                    path=path.resolve().relative_to(repo).as_posix(),
                    url=f"{client.base_url}/fdsnws/dataselect/1/query?net={net}&sta={sta}"
                    f"&loc={trace.stats.location or '--'}&cha={trace.stats.channel}"
                    f"&start={start.isoformat()}&end={end.isoformat()}",
                    params={"data_centre": centre, "base_url": str(client.base_url)},
                    sha256=digest,
                    size_bytes=path.stat().st_size,
                    retrieved_at=retrieved_at,
                    licence=LICENCE,
                    licence_source_url=LICENCE_URL,
                    provenance=Provenance.real,
                    status=ManifestStatus.fetched,
                    time_start=start.datetime.replace(tzinfo=UTC),
                    time_end=end.datetime.replace(tzinfo=UTC),
                    adapter=ADAPTER,
                    adapter_version=ADAPTER_VERSION,
                    notes=LICENCE_NOTE,
                )
            )
        print(f"    {net}.{sta}: {len(stream)} channel(s)")

    if not files:
        print("  no waveforms retrieved; nothing written")
        return {"target_id": target.target_id, "files": [], "azimuthal_gap_deg": gap}

    merged = inventories[0]
    for inventory in inventories[1:]:
        merged += inventory
    # Response-level StationXML is 90% of the fixture bytes and compresses about 20x.
    # ObsPy's reader decompresses transparently, so the committed form is gzipped.
    station_path = out / "stations.xml.gz"
    raw = io.BytesIO()
    merged.write(raw, format="STATIONXML")
    with gzip.GzipFile(filename="", fileobj=station_path.open("wb"), mode="wb", mtime=0) as fh:
        fh.write(raw.getvalue())
    station_digest = sha256_of_file(station_path)
    ledger.append(
        ManifestEntry(
            source=DataSource.fdsn_waveforms,
            product_id=f"lfh/{target.target_id}/stations.xml.gz",
            product_level="stationxml",
            path=station_path.resolve().relative_to(repo).as_posix(),
            url="fdsnws-station level=response, merged across data centres",
            params={"data_centres": sorted({str(f["data_centre"]) for f in files})},
            sha256=station_digest,
            size_bytes=station_path.stat().st_size,
            retrieved_at=retrieved_at,
            licence=LICENCE,
            licence_source_url=LICENCE_URL,
            provenance=Provenance.real,
            status=ManifestStatus.fetched,
            adapter=ADAPTER,
            adapter_version=ADAPTER_VERSION,
            notes=LICENCE_NOTE,
        )
    )

    manifest = {
        "target_id": target.target_id,
        "origin_utc": target.origin_utc + "Z",
        "source_latitude": target.latitude,
        "source_longitude": target.longitude,
        "window": {
            "start_utc": start.datetime.replace(tzinfo=UTC).isoformat(),
            "end_utc": end.datetime.replace(tzinfo=UTC).isoformat(),
        },
        "n_candidates": len(candidates),
        "azimuthal_gap_deg": round(gap, 1),
        "licence": None,
        "licence_source_url": LICENCE_URL,
        "notes": LICENCE_NOTE,
        "stations_xml_sha256": station_digest,
        "files": sorted(files, key=lambda f: str(f["sncl"])),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(int(f["size_bytes"]) for f in files) + station_path.stat().st_size  # type: ignore[arg-type]
    print(f"  wrote {len(files)} traces + StationXML, {total / 1024:.0f} kB")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--target", action="append", default=None)
    args = parser.parse_args()
    repo: Path = args.repo.resolve()
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    wanted = set(args.target) if args.target else None
    for target in load_targets(repo):
        if wanted and target.target_id not in wanted:
            continue
        fetch(target, repo, ledger)


if __name__ == "__main__":
    main()
