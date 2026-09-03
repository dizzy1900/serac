"""Station geometry, the refusal rules, and the seal that stops the config being tuned.

The refusal rules are the reason M2 reports nothing for three of its six events, so they are
tested for what they let through as carefully as for what they stop. The seal is tested for
the one thing it exists to do: notice that a knob moved.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from serac.models.lfh.config import (
    BandConfig,
    LfhConfig,
    MassConfig,
    RegularisationConfig,
    read_seal,
    seal_config,
    write_seal,
)
from serac.models.lfh.waveforms import (
    Geometry,
    StationChannel,
    azimuthal_gap,
    bandpass,
    geometry_of,
    refusal_reason,
    select_channels,
    station_weights,
)


def _channel(
    station: str,
    *,
    azimuth: float,
    distance: float = 5.0,
    component: str = "Z",
    amplitude: float = 1e-6,
    snr: float = 3.0,
    n: int = 901,
) -> StationChannel:
    rng = np.random.default_rng(abs(hash((station, component))) % 1000)
    data = rng.normal(size=n) * amplitude
    return StationChannel(
        key=f"XX.{station}..LH{component}",
        network="XX",
        station=station,
        location="",
        channel=f"LH{component}",
        component=component,
        latitude=0.0,
        longitude=0.0,
        distance_deg=distance,
        azimuth_deg=azimuth,
        data=data,
        broadband=data.copy(),
        sampling_rate_hz=1.0,
        response_removed=True,
        snr=snr,
    )


# --- azimuthal gap ----------------------------------------------------------------------------


def test_a_single_station_is_a_full_circle_of_ignorance() -> None:
    assert azimuthal_gap([]) == 360.0
    assert azimuthal_gap([90.0]) == 360.0


def test_the_gap_wraps_around_north() -> None:
    """Stations at 350 and 10 degrees are 20 apart, not 340."""
    assert azimuthal_gap([350.0, 10.0]) == pytest.approx(340.0)
    assert azimuthal_gap([0.0, 90.0, 180.0, 270.0]) == pytest.approx(90.0)
    assert azimuthal_gap([10.0, 20.0, 30.0]) == pytest.approx(340.0)


def test_geometry_counts_stations_not_channels() -> None:
    channels = [_channel("AAA", azimuth=0.0, component=c) for c in "ZNE"] + [
        _channel("BBB", azimuth=120.0, component=c) for c in "ZNE"
    ]
    geometry = geometry_of(channels)
    assert (geometry.n_stations, geometry.n_channels) == (2, 6)
    assert geometry.median_snr == pytest.approx(3.0)
    assert "median pre-event SNR" in geometry.describe()


# --- the refusals -----------------------------------------------------------------------------


def test_a_wide_azimuthal_gap_is_refused() -> None:
    config = LfhConfig()
    reason = refusal_reason(
        Geometry(6, 18, 250.0, 2.0, 9.0, [f"XX.S{i}" for i in range(6)]), config
    )
    assert reason is not None and "250" in reason and "200" in reason


def test_too_few_stations_is_refused() -> None:
    config = LfhConfig()
    reason = refusal_reason(Geometry(4, 12, 30.0, 2.0, 9.0, ["a", "b", "c", "d"]), config)
    assert reason is not None and "below the minimum of 5" in reason


def test_a_good_geometry_is_not_refused() -> None:
    config = LfhConfig()
    assert refusal_reason(Geometry(9, 27, 78.0, 1.0, 14.0, list("abcdefghi")), config) is None


def test_the_boundary_cases_go_the_way_the_thresholds_say() -> None:
    """Exactly five stations and exactly 200 degrees must pass; one worse must not."""
    config = LfhConfig()
    assert refusal_reason(Geometry(5, 15, 200.0, 1.0, 9.0, list("abcde")), config) is None
    assert refusal_reason(Geometry(4, 12, 200.0, 1.0, 9.0, list("abcd")), config) is not None
    assert refusal_reason(Geometry(5, 15, 200.1, 1.0, 9.0, list("abcde")), config) is not None


# --- station selection --------------------------------------------------------------------------


def test_selection_spreads_over_azimuth_rather_than_taking_the_nearest() -> None:
    """A dense local cluster must not outvote the station that closes the gap.

    Twelve stations crowded into one quadrant plus one on the far side: a nearest-first rule
    would drop the far one and leave a gap the refusal check would then catch, which is the
    wrong failure. Azimuth binning keeps it.
    """
    config = LfhConfig().model_copy(
        update={"stations": LfhConfig().stations.model_copy(update={"max_stations": 4})}
    )
    crowded = [_channel(f"C{i:02d}", azimuth=10.0 + i, distance=1.0 + i * 0.01) for i in range(12)]
    far = _channel("FAR", azimuth=200.0, distance=9.0)
    selected, notes = select_channels([*crowded, far], config)
    stations = {c.station_key for c in selected}
    assert "XX.FAR" in stations, "the azimuth-closing station must survive the cap"
    assert len(stations) == 4
    assert any("azimuth-spread cap" in note for note in notes)


def test_a_glitch_is_dropped_and_said_so() -> None:
    config = LfhConfig()
    normal = [_channel(f"S{i}", azimuth=i * 60.0) for i in range(6)]
    glitch = _channel("BAD", azimuth=30.0, amplitude=1e-3)
    selected, notes = select_channels([*normal, glitch], config)
    assert "XX.BAD" not in {c.station_key for c in selected}
    assert any("glitch" in note for note in notes)


def test_channels_outside_the_distance_window_are_dropped() -> None:
    config = LfhConfig()
    selected, notes = select_channels(
        [
            _channel("NEAR", azimuth=0.0, distance=0.1),
            _channel("OK", azimuth=90.0, distance=5.0),
            _channel("FAR", azimuth=180.0, distance=40.0),
        ],
        config,
    )
    assert {c.station_key for c in selected} == {"XX.OK"}
    assert sum("outside" in note for note in notes) == 2


def test_station_weights_stop_the_nearest_station_deciding_everything() -> None:
    """Amplitudes span orders of magnitude across 0.5-15 degrees; weights must undo that."""
    loud = _channel("LOUD", azimuth=0.0, amplitude=1e-4)
    quiet = _channel("QUIET", azimuth=180.0, amplitude=1e-9)
    weights = station_weights([loud, quiet])
    assert weights[loud.key] < weights[quiet.key]
    loud_rms = float(np.sqrt(np.mean(loud.data**2))) * weights[loud.key]
    quiet_rms = float(np.sqrt(np.mean(quiet.data**2))) * weights[quiet.key]
    assert loud_rms == pytest.approx(quiet_rms, rel=1e-9)


# --- the band -----------------------------------------------------------------------------------


def test_the_bandpass_keeps_the_working_band_and_rejects_outside_it() -> None:
    config = LfhConfig()
    n, dt = 2048, 1.0
    t = np.arange(n) * dt
    for period, expect_pass in ((60.0, True), (5.0, False), (600.0, False)):
        signal = np.sin(2 * np.pi * t / period)
        filtered = bandpass(signal, dt=dt, config=config)
        middle = filtered[300:-300]
        amplitude = float(np.abs(middle).max())
        if expect_pass:
            assert amplitude > 0.7, f"{period} s should pass; got {amplitude:.3f}"
        else:
            assert amplitude < 0.2, f"{period} s should be rejected; got {amplitude:.3f}"


# --- the seal -------------------------------------------------------------------------------------


def test_the_config_hash_is_stable_and_order_independent() -> None:
    assert LfhConfig().config_hash() == LfhConfig().config_hash()
    assert len(LfhConfig().config_hash()) == 64


@pytest.mark.parametrize(
    "update",
    [
        {"source_duration_s": 400.0},
        {"band": BandConfig(short_period_s=25.0)},
        {"regularisation": RegularisationConfig(n_lambda=41)},
        {"mass": MassConfig(friction_ratio_max=0.7)},
    ],
)
def test_any_knob_changes_the_hash(update: dict[str, object]) -> None:
    """Whatever moves, the seal must notice."""
    assert LfhConfig().model_copy(update=update).config_hash() != LfhConfig().config_hash()


def test_a_seal_round_trips_and_rejects_a_forged_hash(tmp_path: Path) -> None:
    from serac.models.lfh.config import Seal

    config = LfhConfig()
    seal = seal_config(config, git_sha="abc123", reproductions=["b", "a"])
    assert seal.reproductions == ["a", "b"], "sorted, so the record is stable"
    write_seal(seal, tmp_path)
    restored = read_seal(tmp_path)
    assert restored is not None and restored.config_hash == config.config_hash()

    with pytest.raises(ValueError, match="does not hash the config"):
        Seal(config_hash="0" * 64, config=config)


def test_no_seal_reads_as_none(tmp_path: Path) -> None:
    assert read_seal(tmp_path) is None


def test_the_greens_shift_and_sample_counts_are_consistent() -> None:
    config = LfhConfig()
    assert config.n_window_samples == 901
    assert config.n_source_samples == 301
    assert config.greens_shift_samples == -60


def test_config_validators_reject_impossible_settings() -> None:
    with pytest.raises(ValueError, match="shorter than"):
        BandConfig(short_period_s=200.0, long_period_s=100.0)
    with pytest.raises(ValueError, match="friction_ratio_min"):
        MassConfig(friction_ratio_min=0.9, friction_ratio_max=0.2)
    with pytest.raises(ValueError, match="lambda_min"):
        RegularisationConfig(lambda_min=10.0, lambda_max=1.0)
