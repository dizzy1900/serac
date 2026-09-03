"""The event-library index: `data/events/events.parquet` derived from the JSON records.

One GeoParquet row per `data/events/<event_id>.json`, EPSG:4326 point geometry at the source
location, every `Range` flattened to `<name>_low/_high/_best` (all None when the range is
null) and `json_sha256` of the record file so `index_is_stale` can tell when a record changed
without rebuilding. The index is derived data: the JSON records are the truth and the index is
rebuilt by `serac events build-index`; `make validate-events` fails when it drifts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from serac.adapters.storage.manifest_ledger import sha256_of_file
from serac.domain.common import Range
from serac.domain.events import MassMovementEvent

INDEX_FILENAME = "events.parquet"

RANGE_COLUMNS: dict[str, str] = {
    "source_elevation": "source_elevation_m",
    "fall_height": "fall_height_m",
    "source_volume": "source_volume_m3",
    "rock_fraction": "rock_fraction",
    "bulked_volume": "bulked_volume_m3",
    "runout": "runout_km",
    "peak_velocity": "peak_velocity_ms",
    "fatalities": "fatalities",
}
"""Index column prefix -> `MassMovementEvent` field name."""

INDEX_COLUMNS: tuple[str, ...] = (
    "event_id",
    "name",
    "event_group",
    "role",
    "aoi_id",
    "failure_type",
    "time_utc",
    "time_basis",
    "lat",
    "lon",
    *(f"{prefix}_{part}" for prefix in RANGE_COLUMNS for part in ("low", "high", "best")),
    "seismic_usgs_id",
    "seismic_magnitude_best",
    "dammed_river",
    "secondary_surge",
    "n_sources",
    "json_sha256",
)


def record_paths(events_dir: Path) -> list[Path]:
    """Every `*.json` under `events_dir`, sorted by name (the index is never a record)."""
    if not events_dir.is_dir():
        return []
    return sorted(p for p in events_dir.glob("*.json") if p.is_file())


def load_records(events_dir: Path) -> list[tuple[Path, MassMovementEvent]]:
    """Parse and validate every record; raises on the first invalid one."""
    return [
        (path, MassMovementEvent.model_validate_json(path.read_bytes()))
        for path in record_paths(events_dir)
    ]


def _range_parts(rng: Range | None) -> tuple[float | None, float | None, float | None]:
    if rng is None:
        return None, None, None
    return rng.low, rng.high, rng.best


def index_row(path: Path, event: MassMovementEvent) -> dict[str, Any]:
    """The flattened index row for one record."""
    row: dict[str, Any] = {
        "event_id": event.event_id,
        "name": event.name,
        "event_group": event.event_group,
        "role": event.role.value,
        "aoi_id": event.aoi_id,
        "failure_type": event.failure_type.value,
        "time_utc": event.time.datetime_utc,
        "time_basis": event.time.basis,
        "lat": event.source_location.lat,
        "lon": event.source_location.lon,
    }
    for prefix, field_name in RANGE_COLUMNS.items():
        low, high, best = _range_parts(getattr(event, field_name))
        row[f"{prefix}_low"] = low
        row[f"{prefix}_high"] = high
        row[f"{prefix}_best"] = best
    seismic = event.seismic
    row["seismic_usgs_id"] = seismic.usgs_id if seismic is not None else None
    row["seismic_magnitude_best"] = (
        seismic.magnitude.best if seismic is not None and seismic.magnitude is not None else None
    )
    row["dammed_river"] = event.dammed_river
    row["secondary_surge"] = event.secondary_surge
    row["n_sources"] = len(event.sources)
    row["json_sha256"] = sha256_of_file(path)
    return row


def build_frame(records: list[tuple[Path, MassMovementEvent]]) -> gpd.GeoDataFrame:
    """A GeoDataFrame (EPSG:4326 points) with one row per record, sorted by event_id."""
    rows = sorted((index_row(path, event) for path, event in records), key=lambda r: r["event_id"])
    frame = pd.DataFrame(rows, columns=list(INDEX_COLUMNS))
    frame["time_utc"] = pd.to_datetime(frame["time_utc"], utc=True)
    for column in ("lat", "lon"):
        frame[column] = frame[column].astype("float64")
    geometry = gpd.points_from_xy(frame["lon"], frame["lat"], crs="EPSG:4326")
    return gpd.GeoDataFrame(frame, geometry=geometry, crs="EPSG:4326")


def build_index(events_dir: Path, out: Path | None = None) -> Path:
    """Rebuild the GeoParquet index from the records and return its path."""
    target = out or events_dir / INDEX_FILENAME
    frame = build_frame(load_records(events_dir))
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target, index=False)
    return target


def current_hashes(events_dir: Path) -> dict[str, str]:
    """`event_id -> sha256` of every record file, read without full validation.

    A record whose JSON lacks a usable `event_id` is keyed by its file stem so that it still
    shows up as a difference against the index.
    """
    hashes: dict[str, str] = {}
    for path in record_paths(events_dir):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            loaded = None
        event_id = loaded.get("event_id") if isinstance(loaded, dict) else None
        key = event_id if isinstance(event_id, str) and event_id else path.stem
        hashes[key] = sha256_of_file(path)
    return hashes


def indexed_hashes(index_path: Path) -> dict[str, str]:
    """`event_id -> json_sha256` as stored in the index."""
    frame = pd.read_parquet(index_path, columns=["event_id", "json_sha256"])
    return {
        str(event_id): str(digest)
        for event_id, digest in zip(
            frame["event_id"].tolist(), frame["json_sha256"].tolist(), strict=True
        )
    }


def index_is_stale(events_dir: Path, index_path: Path | None = None) -> bool:
    """True when the index is missing, lists a different set of records, or any record changed."""
    index = index_path or events_dir / INDEX_FILENAME
    if not index.is_file():
        return True
    return indexed_hashes(index) != current_hashes(events_dir)
