"""Per-unit aggregation, on a fictional MintPy stack written by the test itself."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from serac.errors import SeracError
from serac.models.watch.aggregate import (
    MIN_PIXEL_TEMPORAL_COHERENCE,
    MIN_PIXELS_PER_UNIT,
    TIMESERIES_PREFERENCE,
    build_unit_cube,
    days_since_epoch,
    epoch_plus,
    select_timeseries,
    write_watch_cube,
)


def _write_timeseries(path: Path, series_m: np.ndarray, dates: list[str]) -> Path:
    import h5py

    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as fh:
        fh.create_dataset("timeseries", data=series_m.astype(np.float32))
        fh.create_dataset("date", data=np.array([d.encode() for d in dates]))
        fh.attrs["WIDTH"] = str(series_m.shape[2])
    return path


def _write_coherence(path: Path, coherence: np.ndarray) -> Path:
    import h5py

    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as fh:
        fh.create_dataset("temporalCoherence", data=coherence.astype(np.float32))
    return path


@pytest.fixture
def stack(tmp_path: Path) -> tuple[Path, Path, np.ndarray, dict[int, str]]:
    """Two units of 10 pixels each; unit 1 is coherent, unit 2 is not."""
    labels = np.zeros((4, 5), dtype=np.int32)
    labels[:2, :] = 1
    labels[2:, :] = 2
    coherence = np.zeros((4, 5), dtype=np.float32)
    coherence[:2, :] = 0.8
    coherence[2:, :] = 0.1
    # Unit 1 drifts 1 mm per epoch; unit 2 is noise nobody should ever see.
    series = np.zeros((5, 4, 5), dtype=np.float32)
    for t in range(5):
        series[t, :2, :] = t * 0.001  # metres
        series[t, 2:, :] = 99.0
    ts = _write_timeseries(
        tmp_path / "timeseries.h5", series, [f"2020010{t + 1}" for t in range(5)]
    )
    coh = _write_coherence(tmp_path / "temporalCoherence.h5", coherence)
    return ts, coh, labels, {1: "su-1", 2: "su-2"}


def test_the_cube_reports_mm_and_the_median_over_coherent_pixels(
    stack: tuple[Path, Path, np.ndarray, dict[int, str]],
) -> None:
    ts, coh, labels, unit_ids = stack
    cube = build_unit_cube(
        timeseries_h5=ts,
        temporal_coherence_h5=coh,
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed={"su-1": 0.8, "su-2": 0.8},
    )
    assert cube.unit_ids == ["su-1", "su-2"]
    assert cube.los_mm.shape == (2, 5)
    # MintPy hands back metres; the cube is millimetres.
    assert cube.los_mm[0].tolist() == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0])


def test_a_unit_with_no_coherent_pixel_is_nan_not_zero(
    stack: tuple[Path, Path, np.ndarray, dict[int, str]],
) -> None:
    ts, coh, labels, unit_ids = stack
    cube = build_unit_cube(
        timeseries_h5=ts,
        temporal_coherence_h5=coh,
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed={"su-1": 0.8, "su-2": 0.8},
    )
    assert np.isnan(cube.los_mm[1]).all(), "an incoherent unit must not get a value of zero"
    assert cube.n_pixels_valid[1].max() == 0
    assert cube.n_pixels_total[1] == 10


def test_pixel_counts_distinguish_not_measured_from_measured_as_zero(
    stack: tuple[Path, Path, np.ndarray, dict[int, str]],
) -> None:
    ts, coh, labels, unit_ids = stack
    cube = build_unit_cube(
        timeseries_h5=ts,
        temporal_coherence_h5=coh,
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed={"su-1": 0.8, "su-2": 0.8},
    )
    assert cube.n_pixels_valid[0, 0] == 10
    assert cube.los_mm[0, 0] == pytest.approx(0.0)  # measured, and it is zero
    assert cube.n_pixels_valid[1, 0] == 0
    assert np.isnan(cube.los_mm[1, 0])  # not measured


def test_coherence_loss_is_the_fraction_below_the_anomaly_floor(
    stack: tuple[Path, Path, np.ndarray, dict[int, str]],
) -> None:
    ts, coh, labels, unit_ids = stack
    cube = build_unit_cube(
        timeseries_h5=ts,
        temporal_coherence_h5=coh,
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed={"su-1": 0.8, "su-2": 0.8},
    )
    assert cube.coherence_loss[0, 0] == pytest.approx(0.0)
    assert cube.coherence_loss[1, 0] == pytest.approx(1.0)


def test_a_grid_mismatch_is_an_error_not_a_silent_crop(tmp_path: Path) -> None:
    ts = _write_timeseries(
        tmp_path / "timeseries.h5", np.zeros((2, 4, 5), dtype=np.float32), ["20200101", "20200102"]
    )
    coh = _write_coherence(tmp_path / "temporalCoherence.h5", np.zeros((4, 5), dtype=np.float32))
    with pytest.raises(SeracError, match="does not match"):
        build_unit_cube(
            timeseries_h5=ts,
            temporal_coherence_h5=coh,
            labels=np.ones((6, 7), dtype=np.int32),
            unit_ids={1: "su-1"},
            los_sensitivity_signed={"su-1": 0.8},
        )


def test_the_cube_round_trips_through_zarr_with_its_provenance(
    tmp_path: Path, stack: tuple[Path, Path, np.ndarray, dict[int, str]]
) -> None:
    import xarray as xr

    ts, coh, labels, unit_ids = stack
    cube = build_unit_cube(
        timeseries_h5=ts,
        temporal_coherence_h5=coh,
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed={"su-1": 0.8, "su-2": 0.8},
    )
    out = write_watch_cube(
        cube,
        tmp_path / "watch_cube.zarr",
        provenance={
            "aoi_id": "test-aoi",
            "path_number": 56,
            "mintpy_config_sha256": "a" * 64,
            "delineation_sha256": "b" * 64,
            "network_plan_sha256": "c" * 64,
            "timeseries_file": "timeseries_tropHgt_ramp_demErr.h5",
            "corrections_applied": "troposphere, ramp, DEM error",
        },
    )
    dataset = xr.open_zarr(out, consolidated=False)
    assert list(dataset.sizes) == ["unit", "time"] or set(dataset.sizes) == {"unit", "time"}
    assert dataset["los_displacement"].attrs["units"] == "mm"
    assert dataset["los_displacement"].attrs["delineation_sha256"] == "b" * 64
    assert (
        dataset["los_displacement"].attrs["corrections_applied"] == "troposphere, ramp, DEM error"
    )
    assert "not a calibrated failure probability" in dataset.attrs["tier_disclaimer"]
    assert dataset["n_pixels_valid"].attrs["long_name"]
    assert dataset["coherence_loss"].attrs["note"].startswith("a data-quality variable")


# -- time-series selection ------------------------------------------------------------------


def test_the_most_corrected_time_series_wins(tmp_path: Path) -> None:
    """Reading `timeseries.h5` would silently discard every correction MintPy applied."""
    for name in ("timeseries.h5", "timeseries_tropHgt.h5", "timeseries_tropHgt_ramp.h5"):
        (tmp_path / name).write_bytes(b"")
    assert select_timeseries(tmp_path).name == "timeseries_tropHgt_ramp.h5"
    (tmp_path / "timeseries_tropHgt_ramp_demErr.h5").write_bytes(b"")
    assert select_timeseries(tmp_path).name == "timeseries_tropHgt_ramp_demErr.h5"


def test_selection_falls_back_to_the_raw_series_when_nothing_else_exists(tmp_path: Path) -> None:
    (tmp_path / "timeseries.h5").write_bytes(b"")
    assert select_timeseries(tmp_path).name == "timeseries.h5"


def test_selection_raises_when_there_is_no_time_series(tmp_path: Path) -> None:
    with pytest.raises(SeracError, match="no MintPy time series"):
        select_timeseries(tmp_path)


def test_the_preference_order_is_most_corrected_first() -> None:
    assert TIMESERIES_PREFERENCE[0] == "timeseries_tropHgt_ramp_demErr.h5"
    assert TIMESERIES_PREFERENCE[-1] == "timeseries.h5"


def test_epoch_helpers_round_trip() -> None:
    when = datetime(2021, 2, 7, tzinfo=UTC)
    assert epoch_plus(days_since_epoch(when)) == when


def test_the_thresholds_are_the_documented_ones() -> None:
    assert MIN_PIXEL_TEMPORAL_COHERENCE == 0.40
    assert MIN_PIXELS_PER_UNIT == 5
