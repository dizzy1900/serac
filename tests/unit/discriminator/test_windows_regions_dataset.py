"""Station selection, the region artefact, the chunk index and the split assignment."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest
from tests.conftest import REPO_ROOT

from serac.models.discriminator.catalog import (
    FORCED_TEST_GROUPS,
    CatalogEntry,
    CatalogSource,
    ClassLabel,
)
from serac.models.discriminator.dataset import (
    DatasetIndex,
    WindowRecord,
    assign_loro,
    assign_time_forward,
    open_store,
    verify_store,
    write_chunk_index,
    write_window,
)
from serac.models.discriminator.regions import (
    HELD_OUT_REGION,
    REGIONS_GEOJSON,
    region_for,
    regions_geojson,
)
from serac.models.discriminator.windows import (
    COMPONENTS,
    MAX_DISTANCE_KM,
    MAX_STATIONS_PER_EVENT,
    MIN_DISTANCE_KM,
    N_SAMPLES,
    azimuth_deg,
    select_stations,
)


def _entry() -> CatalogEntry:
    return CatalogEntry(
        entry_id="pos/g1",
        event_group="g1",
        class_label=ClassLabel.mass_movement,
        origin_utc=datetime(2019, 5, 5, tzinfo=UTC),
        latitude=30.0,
        longitude=80.0,
        region_id="high_mountain_asia",
        source=CatalogSource.esec,
        source_ids=["esec:1"],
        location_basis="esec_crown",
    )


def _channels() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(40):
        bearing = index * 9.0
        offset = 2.0 + index * 0.2  # degrees, so distance spans the annulus
        rows.append(
            {
                "net": "XX",
                "sta": f"S{index:02d}",
                "loc": "",
                "cha": "BHZ",
                "lat": 30.0 + offset * np.cos(np.radians(bearing)),
                "lon": 80.0 + offset * np.sin(np.radians(bearing)),
                "rate": 40.0,
                "centre": "https://service.earthscope.org",
            }
        )
    return rows


def test_station_selection_respects_the_annulus_and_the_cap() -> None:
    chosen = select_stations(_entry(), _channels())
    assert 0 < len(chosen) <= MAX_STATIONS_PER_EVENT
    for station in chosen:
        assert MIN_DISTANCE_KM <= station.distance_km <= MAX_DISTANCE_KM


def test_station_selection_spreads_over_azimuth_before_filling_by_distance() -> None:
    """A single force radiates differently by azimuth; twelve receivers on one side see once."""
    chosen = select_stations(_entry(), _channels())
    assert len({s.azimuth_bin for s in chosen}) >= 5


def test_station_selection_is_deterministic() -> None:
    rows = _channels()
    first = [s.key for s in select_stations(_entry(), rows)]
    second = [s.key for s in select_stations(_entry(), list(reversed(rows)))]
    assert first == second


def test_azimuth_is_measured_clockwise_from_north() -> None:
    assert azimuth_deg(0.0, 0.0, 10.0, 0.0) == pytest.approx(0.0, abs=1e-6)
    assert azimuth_deg(0.0, 0.0, 0.0, 10.0) == pytest.approx(90.0, abs=1e-6)


def test_the_committed_region_geojson_matches_the_code() -> None:
    on_disk = json.loads((REPO_ROOT / REGIONS_GEOJSON).read_text(encoding="utf-8"))
    assert on_disk == regions_geojson()


def test_the_region_file_says_it_is_not_authoritative() -> None:
    """The disclaimer has to travel with the file, not only live in a docstring."""
    document = json.loads((REPO_ROOT / REGIONS_GEOJSON).read_text(encoding="utf-8"))
    assert "NOT an authoritative" in str(document["note"])
    for feature in document["features"]:
        properties = feature["properties"]
        assert properties["authoritative"] is False
        assert properties["purpose"] == "model_evaluation_stratification"


def test_chamoli_and_langtang_fall_in_the_held_out_region() -> None:
    assert region_for(30.3485, 79.7759) == HELD_OUT_REGION
    assert region_for(28.271, 85.515) == HELD_OUT_REGION


def _index(rows: list[tuple[str, int, str, ClassLabel]]) -> DatasetIndex:
    windows = [
        WindowRecord(
            index=i,
            entry_id=f"e{i}",
            event_group=group,
            class_label=label,
            origin_utc=datetime(year, 6, 1, tzinfo=UTC),
            region_id=region,
            decade=f"{year // 10 * 10}s",
            source="esec",
            source_ids=[f"esec:{i}"],
            matched_positive_id=None if label is ClassLabel.mass_movement else f"e{i}p",
            station_keys=["XX.A..B"],
            n_stations=1,
            n_valid_channels=3,
        )
        for i, (group, year, region, label) in enumerate(rows)
    ]
    return DatasetIndex(
        built_at_utc=datetime.now(tz=UTC),
        n_windows=len(windows),
        sampling_rate_hz=20.0,
        bandpass_hz=(0.005, 5.0),
        windows=windows,
    )


def test_no_group_straddles_a_split_and_forced_groups_are_test_only() -> None:
    index = _index(
        [
            ("old", 2005, "european_alps", ClassLabel.mass_movement),
            ("old", 2005, "european_alps", ClassLabel.tectonic),
            ("mid", 2021, "alaska_yukon", ClassLabel.mass_movement),
            ("chamoli-2021", 2021, "high_mountain_asia", ClassLabel.mass_movement),
            ("langtang-lhende-2026", 2026, "high_mountain_asia", ClassLabel.mass_movement),
        ]
    )
    for assignment in (assign_time_forward(index), assign_loro(index, HELD_OUT_REGION)):
        assert set(assignment.by_group) == {"old", "mid", "chamoli-2021", "langtang-lhende-2026"}
        for group in FORCED_TEST_GROUPS & set(assignment.by_group):
            assert assignment.by_group[group] == "test"
        labels = assignment.for_windows(index.windows)
        assert labels[0] == labels[1]  # a group's windows share its split


def test_loro_holds_out_the_whole_region() -> None:
    index = _index(
        [
            ("hma-1", 2010, "high_mountain_asia", ClassLabel.mass_movement),
            ("alps-1", 2010, "european_alps", ClassLabel.mass_movement),
            ("alps-2", 2015, "european_alps", ClassLabel.mass_movement),
            ("chamoli-2021", 2021, "high_mountain_asia", ClassLabel.mass_movement),
        ]
    )
    assignment = assign_loro(index, HELD_OUT_REGION)
    assert assignment.by_group["hma-1"] == "test"
    assert assignment.by_group["chamoli-2021"] == "test"
    assert assignment.by_group["alps-1"] != "test"


def test_a_group_takes_its_epoch_from_its_positive_not_a_negative() -> None:
    """A 2020 aftershock must not drag a 2018 slide into the validation fold."""
    windows = [
        WindowRecord(
            index=0,
            entry_id="p",
            event_group="g",
            class_label=ClassLabel.mass_movement,
            origin_utc=datetime(2018, 1, 1, tzinfo=UTC),
            region_id="european_alps",
            decade="2010s",
            source="esec",
            source_ids=["esec:1"],
            station_keys=[],
            n_stations=0,
            n_valid_channels=0,
        ),
        WindowRecord(
            index=1,
            entry_id="n",
            event_group="g",
            class_label=ClassLabel.tectonic,
            origin_utc=datetime(2020, 1, 1, tzinfo=UTC),
            region_id="european_alps",
            decade="2020s",
            source="comcat_tectonic",
            source_ids=["comcat:x"],
            matched_positive_id="p",
            station_keys=[],
            n_stations=0,
            n_valid_channels=0,
        ),
    ]
    index = DatasetIndex(
        built_at_utc=datetime.now(tz=UTC),
        n_windows=2,
        sampling_rate_hz=20.0,
        bandpass_hz=(0.005, 5.0),
        windows=windows,
    )
    assert assign_time_forward(index).by_group["g"] == "train"


def test_the_chunk_index_pins_the_store_and_notices_a_changed_byte(tmp_path) -> None:
    store = open_store(tmp_path, n_windows=2, mode="w")
    rng = np.random.default_rng(0)
    for row in range(2):
        write_window(
            store,
            row,
            rng.standard_normal((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES)).astype(
                np.float32
            ),
            np.ones((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool),
        )
    _, first_hash, n_files = write_chunk_index(tmp_path)
    assert n_files > 0
    ok, differences = verify_store(tmp_path)
    assert ok and not differences

    write_window(
        store,
        0,
        np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32),
        np.ones((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool),
    )
    ok, differences = verify_store(tmp_path)
    assert not ok and differences
    _, second_hash, _ = write_chunk_index(tmp_path)
    assert second_hash != first_hash
