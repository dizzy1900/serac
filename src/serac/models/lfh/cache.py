"""Content-addressed cache for prepared waveforms, so a warm run does not redo the slow part.

Response removal is the expensive step of an inversion that is otherwise dominated by linear
algebra: for a ten-station event it is roughly a third of the wall clock, and it produces the
same answer every time for the same bytes and the same configuration. Caching it is what makes
the difference between the cold and warm latencies the model card reports.

The key is a hash of everything that could change the result -- the waveform fixture bytes, the
StationXML bytes, the origin and nominal source position, and the parts of the configuration
that touch preparation. Change any of them and the key changes, so a stale entry cannot be
served. The cache is *not* a place to put anything that must be reproducible from source: it
lives under `data/interim/`, it is never committed, and every run works with it deleted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from serac.models.lfh.config import LfhConfig
from serac.models.lfh.waveforms import StationChannel

CACHE_VERSION = "1"
DEFAULT_CACHE_DIR = Path("data/interim/lfh/prepared")


def preparation_key(
    fixture_dir: Path,
    *,
    origin_iso: str,
    source_lat: float,
    source_lon: float,
    config: LfhConfig,
) -> str:
    """Hash of every input that could change the prepared channels."""
    digest = hashlib.sha256()
    digest.update(CACHE_VERSION.encode())
    for path in sorted(fixture_dir.glob("*")):
        if path.is_file() and path.suffix in {".mseed", ".gz", ".xml"}:
            digest.update(path.name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    payload = {
        "origin": origin_iso,
        "lat": round(source_lat, 6),
        "lon": round(source_lon, 6),
        "dt_s": config.dt_s,
        "window_before_s": config.window_before_s,
        "window_after_s": config.window_after_s,
        "band": json.loads(config.band.model_dump_json()),
        "stations": json.loads(config.stations.model_dump_json()),
    }
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.npz"


def load(cache_dir: Path, key: str) -> list[StationChannel] | None:
    """Prepared channels for `key`, or None when the cache has nothing usable.

    Any failure to read is treated as a miss rather than an error: a cache that can make a run
    fail is worse than no cache.
    """
    path = _path(cache_dir, key)
    if not path.exists():
        return None
    try:
        payload = np.load(path, allow_pickle=False)
        meta = json.loads(str(payload["meta"]))
    except (OSError, ValueError, KeyError):
        return None
    out: list[StationChannel] = []
    for index, record in enumerate(meta):
        try:
            out.append(
                StationChannel(
                    key=record["key"],
                    network=record["network"],
                    station=record["station"],
                    location=record["location"],
                    channel=record["channel"],
                    component=record["component"],
                    latitude=record["latitude"],
                    longitude=record["longitude"],
                    distance_deg=record["distance_deg"],
                    azimuth_deg=record["azimuth_deg"],
                    data=np.asarray(payload[f"data_{index}"], dtype=float),
                    broadband=np.asarray(payload[f"broadband_{index}"], dtype=float),
                    sampling_rate_hz=record["sampling_rate_hz"],
                    response_removed=record["response_removed"],
                )
            )
        except KeyError:
            return None
    return out


def store(cache_dir: Path, key: str, channels: list[StationChannel]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    meta = []
    for index, channel in enumerate(channels):
        arrays[f"data_{index}"] = channel.data.astype("float32")
        arrays[f"broadband_{index}"] = channel.broadband.astype("float32")
        meta.append(
            {
                "key": channel.key,
                "network": channel.network,
                "station": channel.station,
                "location": channel.location,
                "channel": channel.channel,
                "component": channel.component,
                "latitude": channel.latitude,
                "longitude": channel.longitude,
                "distance_deg": channel.distance_deg,
                "azimuth_deg": channel.azimuth_deg,
                "sampling_rate_hz": channel.sampling_rate_hz,
                "response_removed": channel.response_removed,
            }
        )
    path = _path(cache_dir, key)
    arrays["meta"] = np.asarray(json.dumps(meta))
    # numpy's stub declares `allow_pickle: bool` before its **kwds, so a splatted array dict
    # is matched against it. The call is correct; only the signature is awkward.
    np.savez_compressed(path, **arrays)  # type: ignore[arg-type]
    return path


def clear(cache_dir: Path) -> int:
    """Remove every cached preparation; returns how many files went."""
    if not cache_dir.exists():
        return 0
    removed = 0
    for path in cache_dir.glob("*.npz"):
        path.unlink()
        removed += 1
    return removed
