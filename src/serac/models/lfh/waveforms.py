"""Turning committed MiniSEED into the displacement traces the inversion consumes.

Everything here is deliberately outside `serac.domain`: instrument response removal produces
displacement in metres, and `SeismicTrace.units` is `Literal["counts"]`, so response-removed
data must never ride the bus (ADR-0009 and the Prompt 1 contract). It lives inside the model.

The band is 20-150 s, which is why 1 sps LH? channels are enough and why the offline fixtures
fit in a megabyte. Response removal, filtering and decimation are applied identically to the
data and to the Green's functions, so convolution and filtering commute and the design matrix
stays exact.

Station selection refuses rather than guesses. Fewer than five contributing stations, or an
azimuthal gap wider than the configured limit, and the caller is told to emit
`status="failed"` with the geometry stated and no location. A location from a 250-degree gap
is a number with no evidence behind it.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from serac.adapters.seismic.syngine import geocentric_distance_azimuth
from serac.errors import SeracError
from serac.models.lfh.config import LfhConfig

#: Channel orientation codes mapped onto the (Z, N, E) frame the inversion works in.
_COMPONENT_BY_CODE = {"Z": "Z", "N": "N", "E": "E", "1": "N", "2": "E"}


class WaveformPreparationError(SeracError):
    """Waveforms could not be prepared for inversion."""


@dataclass(frozen=True)
class StationChannel:
    """One prepared channel: displacement in metres, on the inversion's sample grid."""

    key: str
    network: str
    station: str
    location: str
    channel: str
    component: str
    latitude: float
    longitude: float
    distance_deg: float
    azimuth_deg: float
    data: np.ndarray
    #: The same series before the band-pass, so a bootstrap draw can re-filter at a jittered
    #: band instead of filtering an already-filtered trace twice.
    broadband: np.ndarray
    sampling_rate_hz: float
    response_removed: bool
    #: Event-window RMS over pre-event-window RMS, in the working band. Below about 1 there is
    #: nothing to invert, however well-conditioned the geometry looks.
    snr: float = 0.0

    @property
    def station_key(self) -> str:
        return f"{self.network}.{self.station}"

    @property
    def amplitude(self) -> float:
        return float(np.abs(self.data).max()) if self.data.size else 0.0


def azimuthal_gap(azimuths: list[float]) -> float:
    """Largest gap between consecutive station azimuths, in degrees.

    One station is a 360-degree gap; that is the honest answer, not an error.
    """
    if not azimuths:
        return 360.0
    ordered = sorted(a % 360.0 for a in azimuths)
    if len(ordered) == 1:
        return 360.0
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    gaps.append(ordered[0] + 360.0 - ordered[-1])
    return float(max(gaps))


@dataclass(frozen=True)
class Geometry:
    """What the contributing station set looks like from the source."""

    n_stations: int
    n_channels: int
    azimuthal_gap_deg: float
    min_distance_deg: float
    max_distance_deg: float
    station_keys: list[str]
    median_snr: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.n_stations} stations / {self.n_channels} channels, "
            f"azimuthal gap {self.azimuthal_gap_deg:.0f} deg, "
            f"distance {self.min_distance_deg:.2f}-{self.max_distance_deg:.2f} deg, "
            f"median pre-event SNR {self.median_snr:.2f}"
        )


def geometry_of(channels: list[StationChannel]) -> Geometry:
    by_station: dict[str, float] = {}
    for channel in channels:
        by_station.setdefault(channel.station_key, channel.azimuth_deg)
    distances = [c.distance_deg for c in channels] or [0.0]
    return Geometry(
        n_stations=len(by_station),
        n_channels=len(channels),
        azimuthal_gap_deg=azimuthal_gap(list(by_station.values())),
        min_distance_deg=min(distances),
        max_distance_deg=max(distances),
        station_keys=sorted(by_station),
        median_snr=float(np.median([c.snr for c in channels])) if channels else 0.0,
    )


def refusal_reason(geometry: Geometry, config: LfhConfig) -> str | None:
    """Why the inversion must not emit a location, or None when it may.

    Both tests are geometric and both are checked before any inversion runs, so a refusal
    costs nothing and cannot be argued away by a good-looking fit.
    """
    limits = config.stations
    if geometry.n_stations < limits.min_stations:
        return (
            f"only {geometry.n_stations} station(s) contributed, below the minimum of "
            f"{limits.min_stations}; {geometry.describe()}"
        )
    if geometry.azimuthal_gap_deg > limits.max_azimuthal_gap_deg:
        return (
            f"azimuthal gap {geometry.azimuthal_gap_deg:.0f} deg exceeds the limit of "
            f"{limits.max_azimuthal_gap_deg:.0f} deg; {geometry.describe()}"
        )
    return None


def _pre_filter(config: LfhConfig, sampling_rate_hz: float) -> tuple[float, float, float, float]:
    """Cosine taper corners for response removal, one octave outside the working band."""
    f_low = 1.0 / config.band.long_period_s
    f_high = 1.0 / config.band.short_period_s
    nyquist = sampling_rate_hz / 2.0
    return (f_low / 2.0, f_low, min(f_high, 0.8 * nyquist), min(f_high * 1.25, 0.9 * nyquist))


def bandpass(data: np.ndarray, *, dt: float, config: LfhConfig) -> np.ndarray:
    """The single filter applied to both data and Green's functions."""
    from scipy.signal import butter, filtfilt, sosfilt, sosfiltfilt, tf2sos

    nyquist = 0.5 / dt
    low = (1.0 / config.band.long_period_s) / nyquist
    high = min((1.0 / config.band.short_period_s) / nyquist, 0.99)
    if low >= high:  # pragma: no cover - guarded by BandConfig
        raise WaveformPreparationError(f"degenerate band at dt={dt}: {low} >= {high}")
    numerator, denominator = butter(config.band.corners, [low, high], btype="band")
    sos = tf2sos(numerator, denominator)
    if config.band.zerophase:
        padlen = min(3 * config.band.corners * 3, max(data.size - 1, 0))
        if padlen <= 0:  # pragma: no cover - guarded by window length
            return np.asarray(filtfilt(numerator, denominator, data), dtype=float)
        return np.asarray(sosfiltfilt(sos, data, padlen=padlen), dtype=float)
    return np.asarray(sosfilt(sos, data), dtype=float)


def _resample_to(data: np.ndarray, *, source_dt: float, target_dt: float) -> np.ndarray:
    """Decimate or interpolate onto the inversion's sample grid.

    The data are already band-limited well below the 1 sps Nyquist by this point, so linear
    interpolation onto the target grid introduces nothing at the periods that matter.
    """
    if abs(source_dt - target_dt) < 1e-9:
        return data
    n_out = math.floor((data.size - 1) * source_dt / target_dt) + 1
    source_times = np.arange(data.size) * source_dt
    target_times = np.arange(n_out) * target_dt
    return np.asarray(np.interp(target_times, source_times, data), dtype=float)


def prepare_channels(
    stream: Any,
    inventory: Any,
    *,
    origin_utc: datetime,
    source_lat: float,
    source_lon: float,
    config: LfhConfig,
) -> tuple[list[StationChannel], list[str]]:
    """Response-remove, filter, resample and window every usable channel.

    Returns the prepared channels and a list of human-readable notes about what was dropped
    and why -- those notes go into the report, because a silently discarded station is how a
    geometry quietly becomes worse than it looks.
    """
    from obspy import UTCDateTime

    notes: list[str] = []
    prepared: list[StationChannel] = []
    window_start = origin_utc - timedelta(seconds=config.window_before_s)
    window_end = origin_utc + timedelta(seconds=config.window_after_s)

    for trace in stream:
        stats = trace.stats
        code = str(stats.channel)[-1].upper()
        component = _COMPONENT_BY_CODE.get(code)
        key = f"{stats.network}.{stats.station}.{stats.location}.{stats.channel}"
        if component is None:
            notes.append(f"{key}: orientation code {code!r} not in the Z/N/E frame; dropped")
            continue
        try:
            coordinates = inventory.get_coordinates(key, UTCDateTime(origin_utc.timestamp()))
        except Exception as exc:
            notes.append(f"{key}: no channel metadata ({exc}); dropped")
            continue

        work = trace.copy()
        # A pre-event mean and linear trend would otherwise ring through the long-period band.
        work.detrend("linear")
        work.taper(0.05, type="cosine")
        response_removed = True
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                work.remove_response(
                    inventory=inventory,
                    output="DISP",
                    pre_filt=_pre_filter(config, float(stats.sampling_rate)),
                    water_level=60,
                    zero_mean=True,
                    taper=True,
                )
        except Exception as exc:
            notes.append(f"{key}: response removal failed ({exc}); dropped")
            continue

        work.trim(
            UTCDateTime(window_start.timestamp()),
            UTCDateTime(window_end.timestamp()),
            pad=True,
            fill_value=0.0,
        )
        samples = _resample_to(
            np.asarray(work.data, dtype=float),
            source_dt=1.0 / float(work.stats.sampling_rate),
            target_dt=config.dt_s,
        )
        n_target = config.n_window_samples
        if samples.size < n_target:
            samples = np.pad(samples, (0, n_target - samples.size))
        samples = samples[:n_target]
        if not np.isfinite(samples).all():
            notes.append(f"{key}: non-finite samples after response removal; dropped")
            continue
        broadband = samples.copy()
        samples = bandpass(samples, dt=config.dt_s, config=config)
        if float(np.abs(samples).max()) <= 0.0:
            notes.append(
                f"{key}: no energy in the {config.band.short_period_s:g}-"
                f"{config.band.long_period_s:g} s band; dropped"
            )
            continue

        distance_deg, azimuth_deg = geocentric_distance_azimuth(
            source_lat, source_lon, float(coordinates["latitude"]), float(coordinates["longitude"])
        )
        lead = round(config.window_before_s / config.dt_s)
        noise_rms = float(np.sqrt(np.mean(samples[:lead] ** 2))) if lead > 1 else 0.0
        signal_rms = float(np.sqrt(np.mean(samples[lead:] ** 2)))
        snr = signal_rms / noise_rms if noise_rms > 0 else 0.0
        prepared.append(
            StationChannel(
                key=key,
                network=str(stats.network),
                station=str(stats.station),
                location=str(stats.location),
                channel=str(stats.channel),
                component=component,
                latitude=float(coordinates["latitude"]),
                longitude=float(coordinates["longitude"]),
                distance_deg=distance_deg,
                azimuth_deg=azimuth_deg,
                data=samples,
                broadband=broadband,
                sampling_rate_hz=1.0 / config.dt_s,
                response_removed=response_removed,
                snr=snr,
            )
        )
    return prepared, notes


def select_channels(
    channels: list[StationChannel], config: LfhConfig
) -> tuple[list[StationChannel], list[str]]:
    """Distance window, glitch rejection, then an azimuth-spread cap on station count.

    The cap keeps a dense local cluster from outvoting the one station that closes the
    azimuthal gap: stations are sorted into azimuth bins and taken one per bin before any bin
    is revisited.
    """
    notes: list[str] = []
    limits = config.stations

    within = []
    for channel in channels:
        if not limits.min_distance_deg <= channel.distance_deg <= limits.max_distance_deg:
            notes.append(
                f"{channel.key}: {channel.distance_deg:.2f} deg outside "
                f"{limits.min_distance_deg}-{limits.max_distance_deg} deg; dropped"
            )
            continue
        within.append(channel)
    if not within:
        return [], notes

    amplitudes = np.array([c.amplitude for c in within])
    median = float(np.median(amplitudes[amplitudes > 0])) if (amplitudes > 0).any() else 0.0
    kept: list[StationChannel] = []
    for channel in within:
        if median > 0 and channel.amplitude > limits.amplitude_outlier_factor * median:
            notes.append(
                f"{channel.key}: peak amplitude {channel.amplitude:.3e} m is "
                f"{channel.amplitude / median:.0f}x the median; dropped as a glitch"
            )
            continue
        kept.append(channel)

    by_station: dict[str, list[StationChannel]] = {}
    for channel in kept:
        by_station.setdefault(channel.station_key, []).append(channel)
    if len(by_station) <= limits.max_stations:
        return kept, notes

    n_bins = limits.max_stations
    binned: dict[int, list[str]] = {}
    for station, group in by_station.items():
        index = int(group[0].azimuth_deg % 360.0 // (360.0 / n_bins))
        binned.setdefault(index, []).append(station)
    chosen: list[str] = []
    round_index = 0
    while len(chosen) < limits.max_stations:
        added = False
        for index in sorted(binned):
            members = sorted(
                binned[index], key=lambda s: min(c.distance_deg for c in by_station[s])
            )
            if round_index < len(members) and len(chosen) < limits.max_stations:
                chosen.append(members[round_index])
                added = True
        if not added:
            break
        round_index += 1
    dropped = sorted(set(by_station) - set(chosen))
    if dropped:
        notes.append(
            f"azimuth-spread cap at {limits.max_stations} stations dropped {len(dropped)}: "
            + ", ".join(dropped)
        )
    selected = [c for c in kept if c.station_key in set(chosen)]
    return selected, notes


def station_weights(channels: list[StationChannel]) -> dict[str, float]:
    """One over the RMS of each trace, so a near station does not outvote a far one.

    Amplitude falls off by orders of magnitude across 0.5-15 degrees. Without normalisation
    the nearest station would set the answer on its own and the azimuthal coverage that makes
    the force direction resolvable would count for nothing.
    """
    weights: dict[str, float] = {}
    for channel in channels:
        rms = float(np.sqrt(np.mean(channel.data**2)))
        weights[channel.key] = (1.0 / rms) if rms > 0 else 0.0
    total = sum(weights.values())
    if total > 0:
        scale = len(weights) / total
        weights = {key: value * scale for key, value in weights.items()}
    return weights


def read_event_waveforms(fixture_dir: Path) -> tuple[Any, Any]:
    """`(Stream, Inventory)` from a committed fixture directory."""
    from obspy import Stream, read, read_inventory

    if not fixture_dir.exists():
        raise WaveformPreparationError(f"no waveform fixtures at {fixture_dir}")
    station_file = fixture_dir / "stations.xml.gz"
    if not station_file.exists():
        station_file = fixture_dir / "stations.xml"
    if not station_file.exists():
        raise WaveformPreparationError(f"no StationXML (or stations.xml.gz) under {fixture_dir}")
    stream = Stream()
    for path in sorted(fixture_dir.glob("*.mseed")):
        stream += read(str(path), format="MSEED")
    if len(stream) == 0:
        raise WaveformPreparationError(f"no MiniSEED under {fixture_dir}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        inventory = read_inventory(str(station_file), format="STATIONXML")
    return stream, inventory
