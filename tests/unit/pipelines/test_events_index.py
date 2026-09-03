"""`events.parquet`: built from fictional records, readable as GeoParquet, staleness detected."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd
import pandas as pd

from serac.pipelines.events_index import (
    INDEX_COLUMNS,
    INDEX_FILENAME,
    build_index,
    current_hashes,
    index_is_stale,
    load_records,
)
from serac.pipelines.sources import dump_record

if TYPE_CHECKING:
    from tests.unit.conftest import Fictional


def test_build_index_writes_geoparquet(tmp_path: Path, fictional: Fictional) -> None:
    events_dir = tmp_path / "events"
    fictional.write(
        events_dir,
        fictional.event("test-event-2", aoi_id="test-aoi"),
        fictional.event("test-event-1"),
    )
    path = build_index(events_dir)
    assert path == events_dir / INDEX_FILENAME
    frame = gpd.read_parquet(path)
    assert frame.crs is not None and frame.crs.to_epsg() == 4326
    assert list(frame["event_id"]) == ["test-event-1", "test-event-2"], "sorted by event_id"
    assert set(INDEX_COLUMNS) <= set(frame.columns)
    assert list(frame.geometry.x) == [2.0, 2.0]
    assert list(frame.geometry.y) == [1.0, 1.0]
    assert str(frame["time_utc"].dt.tz) == "UTC"
    assert frame["time_utc"].iloc[0] == pd.Timestamp(fictional.time)
    assert frame["fall_height_best"].iloc[0] == 1.5
    assert frame["fall_height_low"].iloc[0] == 1.0
    assert pd.isna(frame["source_volume_best"].iloc[0]), "null Range -> None"
    assert frame["aoi_id"].iloc[1] == "test-aoi"
    assert pd.isna(frame["aoi_id"].iloc[0])
    assert frame["seismic_usgs_id"].iloc[0] == "testid1"
    assert pd.isna(frame["seismic_magnitude_best"].iloc[0])
    assert frame["n_sources"].iloc[0] == 1
    assert bool(frame["dammed_river"].iloc[0]) is False
    assert frame["role"].iloc[0] == "reference"
    hashes = current_hashes(events_dir)
    assert frame["json_sha256"].iloc[0] == hashes["test-event-1"]


def test_load_records_sorted_and_ignores_parquet(tmp_path: Path, fictional: Fictional) -> None:
    events_dir = tmp_path / "events"
    fictional.write(events_dir, fictional.event("test-b"), fictional.event("test-a"))
    build_index(events_dir)
    records = load_records(events_dir)
    assert [path.name for path, _ in records] == ["test-a.json", "test-b.json"]
    assert [event.event_id for _, event in records] == ["test-a", "test-b"]
    assert load_records(tmp_path / "missing") == []


def test_index_is_stale(tmp_path: Path, fictional: Fictional) -> None:
    events_dir = tmp_path / "events"
    assert index_is_stale(events_dir), "no index yet"
    fictional.write(events_dir, fictional.event("test-event-1"), fictional.event("test-event-2"))
    build_index(events_dir)
    assert not index_is_stale(events_dir)

    # A record changes -> the sha differs.
    path = events_dir / "test-event-1.json"
    record = fictional.read(path)
    record["name"] = "Fictional event, renamed"
    path.write_text(dump_record(record), encoding="utf-8")
    assert index_is_stale(events_dir)
    build_index(events_dir)
    assert not index_is_stale(events_dir)

    # The set of records changes.
    fictional.write(events_dir, fictional.event("test-event-3"))
    assert index_is_stale(events_dir)
    (events_dir / "test-event-3.json").unlink()
    assert not index_is_stale(events_dir)

    # The index disappears.
    (events_dir / INDEX_FILENAME).unlink()
    assert index_is_stale(events_dir)


def test_empty_directory_builds_empty_index(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    path = build_index(events_dir)
    frame = gpd.read_parquet(path)
    assert len(frame) == 0
    assert set(INDEX_COLUMNS) <= set(frame.columns)
    assert not index_is_stale(events_dir)


def test_custom_out_path(tmp_path: Path, fictional: Fictional) -> None:
    events_dir = tmp_path / "events"
    fictional.write(events_dir, fictional.event())
    out = tmp_path / "elsewhere" / "idx.parquet"
    assert build_index(events_dir, out) == out
    assert out.exists()
    assert index_is_stale(events_dir), "the default location is still empty"
    assert not index_is_stale(events_dir, out)
