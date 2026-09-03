"""Station selection and waveform windowing for the discriminator.

**The station set belongs to the positive, not to the window.** A positive's stations are
chosen once and its negatives and noise windows are cut at exactly those stations. If they
were not, the classes would differ in instrument, site and network as well as in source
physics, and a model that learned "this is a Nepali broadband, therefore mass movement" would
score beautifully while knowing nothing. `stations_for_group` is the only way windows get
stations, and `serac.validation.discriminator` asserts that every window in a group used the
same set.

**Geometry is used to choose stations and then thrown away.** Distance and azimuth decide
which twelve channels are cut; neither reaches `features.py`, and `FORBIDDEN_FEATURE_TOKENS`
fails the build if a feature name so much as mentions them. Choosing stations by geometry is
sampling; feeding geometry to the classifier would be leakage, because a model can identify a
known event from its epicentral distances alone.

**Response removal is mandatory.** The discriminating quantity is the ratio of long-period to
short-period energy, and a seismometer's response varies by orders of magnitude across
0.005-5 Hz and differs between instrument types. Comparing raw counts from a 120 s Trillium
with raw counts from a 30 s STS-2 measures the instruments, not the ground. A channel with no
response in the inventory is dropped, never processed as counts; `streaming.py` enforces the
same rule at inference time by refusing to score without a response.

Processing chain, fixed in advance: merge and pad gaps -> demean -> linear detrend -> 5%
cosine taper -> `remove_response(output="VEL")` with a `pre_filt` shoulder -> zero-phase
bandpass 0.005-5 Hz -> resample to 20 Hz -> cut to exactly 12000 samples from origin-60 s.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator.catalog import WINDOW_LENGTH_S, CatalogEntry, haversine_km

if TYPE_CHECKING:  # pragma: no cover - obspy is imported lazily inside the functions
    from obspy import Inventory, Stream

WINDOWS_VERSION = "0.1.0"

MIN_DISTANCE_KM: Final = 100.0
MAX_DISTANCE_KM: Final = 1500.0
MAX_STATIONS_PER_EVENT: Final = 12
AZIMUTH_BINS: Final = 6

TARGET_SAMPLING_RATE_HZ: Final = 20.0
BANDPASS_HZ: Final = (0.005, 5.0)
# The response-removal shoulder sits outside the passband so the taper does not bite into it.
PRE_FILT: Final = (0.002, 0.004, 6.0, 8.0)
TAPER_FRACTION: Final = 0.05

N_SAMPLES: Final = int(WINDOW_LENGTH_S * TARGET_SAMPLING_RATE_HZ)
COMPONENTS: Final = ("Z", "N", "E")

# Broadband band codes, in preference order. B is 10-80 Hz (usually 20 or 40), H is >=80 Hz.
# Both resolve the >1 Hz side of the feature set; L (1 Hz) does not and is excluded.
BAND_CODES: Final = ("B", "H")
# ObsPy orients 1/2 to N/E when the inventory gives azimuths; both spellings are accepted.
COMPONENT_ALIASES: Final = {"Z": ("Z",), "N": ("N", "1"), "E": ("E", "2")}

# A window is usable only if this fraction of each kept channel is real, gap-free samples.
MIN_VALID_FRACTION: Final = 0.8
MIN_STATIONS_PER_WINDOW: Final = 3


class WindowError(SeracError):
    """A window could not be cut."""


class MissingResponseError(WindowError):
    """A channel had no instrument response. Counts are never processed as velocity."""


class StationChoice(BaseModel):
    """One station chosen for a positive's group, with the geometry that chose it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    network: str = Field(min_length=1)
    station: str = Field(min_length=1)
    location: str = ""
    band_code: str = Field(min_length=1, max_length=1)
    latitude: float
    longitude: float
    distance_km: float = Field(ge=0)
    azimuth_deg: float = Field(ge=0, lt=360)
    azimuth_bin: int = Field(ge=0)
    sampling_rate_hz: float | None = None
    data_centre: str = ""

    @property
    def key(self) -> str:
        return f"{self.network}.{self.station}.{self.location}.{self.band_code}"

    def channel_code(self, component: str) -> str:
        return f"{self.band_code}H{component}"


def azimuth_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def select_stations(
    entry: CatalogEntry,
    channels: Sequence[dict[str, object]],
    *,
    max_stations: int = MAX_STATIONS_PER_EVENT,
    bins: int = AZIMUTH_BINS,
) -> list[StationChoice]:
    """Up to `max_stations` open broadband stations, spread over azimuth then filled by distance.

    Azimuth binning matters because a single-force source radiates very differently from a
    double-couple one, and that difference is only visible across azimuths. Twelve stations all
    lying north of the source would give the classifier one look at the radiation pattern.

    Ties break on the station key so the selection is deterministic and the dataset rebuilds
    byte-for-byte.
    """
    candidates: dict[str, StationChoice] = {}
    for row in channels:
        band = str(row["cha"])[:1]
        if band not in BAND_CODES:
            continue
        latitude, longitude = float(row["lat"]), float(row["lon"])  # type: ignore[arg-type]
        distance = haversine_km(entry.latitude, entry.longitude, latitude, longitude)
        if not MIN_DISTANCE_KM <= distance <= MAX_DISTANCE_KM:
            continue
        bearing = azimuth_deg(entry.latitude, entry.longitude, latitude, longitude)
        choice = StationChoice(
            network=str(row["net"]),
            station=str(row["sta"]),
            location=str(row.get("loc") or ""),
            band_code=band,
            latitude=latitude,
            longitude=longitude,
            distance_km=distance,
            azimuth_deg=bearing,
            azimuth_bin=int(bearing // (360.0 / bins)),
            sampling_rate_hz=(float(row["rate"]) if row.get("rate") else None),  # type: ignore[arg-type]
            data_centre=str(row.get("centre") or ""),
        )
        # One entry per net.sta.loc.band; prefer B over H so sample rates stay comparable.
        existing = candidates.get(choice.key)
        if existing is None or BAND_CODES.index(band) < BAND_CODES.index(existing.band_code):
            candidates[choice.key] = choice

    # Collapse to one band per net.sta.loc, preferring B.
    per_station: dict[str, StationChoice] = {}
    for choice in candidates.values():
        station_key = f"{choice.network}.{choice.station}.{choice.location}"
        current = per_station.get(station_key)
        if current is None or BAND_CODES.index(choice.band_code) < BAND_CODES.index(
            current.band_code
        ):
            per_station[station_key] = choice

    ordered = sorted(per_station.values(), key=lambda c: (c.distance_km, c.key))
    chosen: list[StationChoice] = []
    used_bins: set[int] = set()
    # Pass 1: nearest station in each azimuth bin.
    for choice in ordered:
        if choice.azimuth_bin not in used_bins:
            chosen.append(choice)
            used_bins.add(choice.azimuth_bin)
        if len(chosen) >= max_stations:
            break
    # Pass 2: fill the remaining slots by distance.
    if len(chosen) < max_stations:
        taken = {c.key for c in chosen}
        for choice in ordered:
            if choice.key in taken:
                continue
            chosen.append(choice)
            if len(chosen) >= max_stations:
                break
    return sorted(chosen, key=lambda c: c.key)


def bulk_rows_for(stations: Sequence[StationChoice], entry: CatalogEntry) -> list[list[str]]:
    """fdsnws bulk rows covering all three components of every chosen station."""
    start = entry.window_start_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    end = entry.window_end_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    return [
        [s.network, s.station, s.location or "--", f"{s.band_code}H?", start, end] for s in stations
    ]


def estimate_window_bytes(stations: Sequence[StationChoice]) -> int:
    """Byte estimate for one window: duration x rate x 3 components x 2 bytes/sample (Steim2).

    The 2 bytes/sample figure is the upper end of Steim2 on broadband counts, so the estimate
    errs high; the basis is printed with every dry run rather than left implicit.
    """
    total = 0.0
    for station in stations:
        rate = station.sampling_rate_hz or 40.0
        total += WINDOW_LENGTH_S * rate * 3 * 2.0
    return int(total)


ESTIMATE_BASIS: Final = (
    f"{WINDOW_LENGTH_S:.0f} s x channel sampling rate x 3 components x 2 bytes/sample "
    "(Steim2 upper bound), summed over the selected stations; rates from fdsnws-station "
    "channel metadata, 40 Hz assumed where absent"
)


def _select_component(stream: Stream, component: str) -> Stream:
    for alias in COMPONENT_ALIASES[component]:
        picked = stream.select(component=alias)
        if len(picked) > 0:
            return picked
    return stream.select(component="__none__")


def process_station_window(
    stream: Stream,
    inventory: Inventory,
    entry: CatalogEntry,
    station: StationChoice,
) -> tuple[np.ndarray, np.ndarray]:
    """One station's window as (3, N_SAMPLES) velocity in m/s plus a (3,) valid mask.

    Raises `MissingResponseError` when the inventory has no response for a component that has
    data: silently returning counts would produce a physically meaningless feature vector that
    nothing downstream could detect.
    """
    from obspy import UTCDateTime

    start = UTCDateTime(entry.window_start_utc.timestamp())
    end = start + WINDOW_LENGTH_S
    out = np.zeros((3, N_SAMPLES), dtype=np.float32)
    valid = np.zeros(3, dtype=bool)

    for index, component in enumerate(COMPONENTS):
        picked = _select_component(stream, component).copy()
        if len(picked) == 0:
            continue
        try:
            picked.merge(method=1, fill_value=None)
        except Exception:
            continue
        if len(picked) != 1:
            continue
        trace = picked[0]
        covered = min(float(trace.stats.endtime), float(end)) - max(
            float(trace.stats.starttime), float(start)
        )
        if covered < MIN_VALID_FRACTION * WINDOW_LENGTH_S:
            continue
        if np.ma.isMaskedArray(trace.data):
            gap_fraction = float(np.ma.getmaskarray(trace.data).mean())
            if gap_fraction > 1.0 - MIN_VALID_FRACTION:
                continue
            trace.data = np.ma.filled(trace.data, 0.0)
        try:
            trace.detrend("demean")
            trace.detrend("linear")
            trace.taper(TAPER_FRACTION, type="cosine")
            trace.remove_response(inventory=inventory, output="VEL", pre_filt=PRE_FILT)
        except Exception as exc:
            text = str(exc)
            if "response" in text.lower() or "matching" in text.lower():
                raise MissingResponseError(
                    f"{station.key}.{component} for {entry.entry_id}: no usable instrument "
                    f"response ({exc}); counts are never scored as velocity"
                ) from exc
            continue
        try:
            trace.filter(
                "bandpass",
                freqmin=BANDPASS_HZ[0],
                freqmax=min(BANDPASS_HZ[1], 0.45 * float(trace.stats.sampling_rate)),
                corners=4,
                zerophase=True,
            )
            if float(trace.stats.sampling_rate) != TARGET_SAMPLING_RATE_HZ:
                trace.resample(TARGET_SAMPLING_RATE_HZ, window="hann", no_filter=False)
            trace.trim(start, end, pad=True, fill_value=0.0)
        except Exception:
            continue
        data = np.asarray(trace.data, dtype=np.float64)
        if data.size < N_SAMPLES:
            data = np.pad(data, (0, N_SAMPLES - data.size))
        sliced = data[:N_SAMPLES]
        if not np.all(np.isfinite(sliced)) or float(np.abs(sliced).max()) == 0.0:
            continue
        out[index] = sliced.astype(np.float32)
        valid[index] = True
    return out, valid
