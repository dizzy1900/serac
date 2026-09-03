"""`DiscriminatorDetector` — the trained baseline behind the `Detector` port.

This is the component built to catch the failure that produced the "M4.4 earthquake" misreport
of 26 August 2026. On the Langtang window as the open archives hold it — two receivers, not the
three this detector requires — it does not yet catch it; `reports/MODEL_CARD_discriminator.md`
carries that case study and its numbers. It accumulates `SeismicTrace` chunks per channel, and
when a window is ready
it removes the instrument response, computes the same 79 features the model was trained on, and
emits a `DetectionCandidate` with a calibrated probability and the contributing channels.

**It refuses to score counts.** `require_response=True` is the default and raises when a
channel has no response in the inventory it was given. This is not fussiness. Every feature the
model uses is a ratio or a shape in velocity, and instrument responses across a broadband
network differ by orders of magnitude and by shape across 0.005-5 Hz. Fed raw counts, the model
would not fail: it would emit a confident, meaningless probability, which is the worst
available behaviour for a component whose output triggers an alert. A loud refusal is the point.

**No location is emitted.** `source_location` stays `None`. M1 says *what kind of source*, not
*where*; locating is M2's job, and `DetectionLocation.method` is deliberately restricted to
`gsf_grid_search` so this detector cannot attach one even by mistake.

`detector_stub.py` is untouched and stays the default until `validate-discriminator` is green;
this detector is added alongside it, as the plan requires.

**Two modes, and why both are measured.** `batch_600s` waits for the full 600 s window the
model was trained on. `sliding_180s` scores a 180 s window zero-padded to 12000 samples as soon
as 180 s have arrived, then re-scores every `stride_s`. The first is the trained configuration;
the second is what a real-time system would have to do, and it is scored by a model that never
saw a truncated window in training. Both latencies are reported. Neither reaches the brief's
60 s budget, and `latency.py`'s report says so rather than picking whichever looks better.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

from serac.domain.detection import ContributingStation, DetectionCandidate
from serac.domain.seismic import SeismicTrace, Sncl, TraceEncoding
from serac.errors import SeracError
from serac.models.discriminator.baseline import ARTIFACT_DIR, CLASSES, LoadedBaseline
from serac.models.discriminator.baseline import load as load_baseline
from serac.models.discriminator.features import FEATURE_NAMES, compute_features
from serac.models.discriminator.windows import (
    BANDPASS_HZ,
    COMPONENT_ALIASES,
    COMPONENTS,
    MAX_STATIONS_PER_EVENT,
    N_SAMPLES,
    PRE_FILT,
    TARGET_SAMPLING_RATE_HZ,
)
from serac.ports.detector import Detector, DetectorInfo

if TYPE_CHECKING:  # pragma: no cover
    from obspy import Inventory, Stream

STREAMING_VERSION = "0.1.0"
DETECTOR_NAME = "discriminator-lgbm"

Mode = Literal["batch_600s", "sliding_180s"]

WINDOW_SECONDS: Final = N_SAMPLES / TARGET_SAMPLING_RATE_HZ
SLIDING_MIN_SECONDS: Final = 180.0
SLIDING_STRIDE_SECONDS: Final = 30.0
DEFAULT_THRESHOLD: Final = 0.5
MIN_CONTRIBUTING_STATIONS: Final = 3
COOLDOWN_SECONDS: Final = 600.0


class DiscriminatorDetectorError(SeracError):
    """The discriminator detector refused an input."""


class ResponseRequiredError(DiscriminatorDetectorError):
    """A channel arrived without an instrument response. Counts are never scored."""


@dataclass
class _Channel:
    """Per-SNCL sample buffer at the channel's own rate, before resampling."""

    sampling_rate_hz: float
    start_utc: datetime
    samples: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    trace_ids: list[str] = field(default_factory=list)

    @property
    def end_utc(self) -> datetime:
        if self.samples.size == 0:
            return self.start_utc
        return self.start_utc + timedelta(seconds=(self.samples.size - 1) / self.sampling_rate_hz)

    @property
    def filled_seconds(self) -> float:
        return self.samples.size / self.sampling_rate_hz


def _component_of(channel_code: str) -> str | None:
    suffix = channel_code[-1:].upper()
    for component, aliases in COMPONENT_ALIASES.items():
        if suffix in aliases:
            return component
    return None


def _station_key(sncl: Sncl) -> str:
    return f"{sncl.network}.{sncl.station}.{sncl.location}.{sncl.channel[:1]}"


class DiscriminatorDetector(Detector):
    """The trained three-class discriminator behind the `ingest`/`poll` port."""

    def __init__(
        self,
        *,
        artifact_dir: Path = ARTIFACT_DIR,
        inventory: Inventory | None = None,
        require_response: bool = True,
        threshold: float = DEFAULT_THRESHOLD,
        mode: Mode = "batch_600s",
        model: LoadedBaseline | None = None,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        self.model = model if model is not None else load_baseline(artifact_dir)
        self.inventory = inventory
        self.require_response = require_response
        self.threshold = threshold
        self.mode = mode
        self.cooldown_seconds = cooldown_seconds
        self._channels: dict[str, _Channel] = {}
        self._last_fired: datetime | None = None
        self._last_scored: datetime | None = None
        self.chunks_seen = 0
        self.windows_scored = 0

    # --- port ---------------------------------------------------------------------------

    def info(self) -> DetectorInfo:
        return DetectorInfo(
            name=DETECTOR_NAME,
            version=STREAMING_VERSION,
            is_stub=False,
            model_sha256=self.model.artifact.model_sha256,
            calibration=self.model.calibrator.method,
            params={
                "mode": self.mode,
                "threshold": self.threshold,
                "window_seconds": WINDOW_SECONDS,
                "sliding_min_seconds": SLIDING_MIN_SECONDS,
                "sliding_stride_seconds": SLIDING_STRIDE_SECONDS,
                "bandpass_hz": f"{BANDPASS_HZ[0]}-{BANDPASS_HZ[1]}",
                "sampling_rate_hz": TARGET_SAMPLING_RATE_HZ,
                "require_response": self.require_response,
                "min_contributing_stations": MIN_CONTRIBUTING_STATIONS,
                "n_features": len(FEATURE_NAMES),
                "trained_on_groups_sha256": self.model.artifact.train_event_groups_sha256,
            },
        )

    def reset(self) -> None:
        self._channels.clear()
        self._last_fired = None
        self._last_scored = None

    def ingest(self, chunk: SeismicTrace) -> None:
        """Buffer one chunk. Refuses counts without a response before a single sample lands."""
        if self.require_response and self.inventory is None:
            raise ResponseRequiredError(
                f"{DETECTOR_NAME} was given no instrument-response inventory and "
                "require_response=True. Scoring raw counts would produce a confident, "
                "physically meaningless probability; pass an Inventory, or set "
                "require_response=False and accept that the probability means nothing."
            )
        if _component_of(chunk.sncl.channel) is None:
            return
        self.chunks_seen += 1
        key = chunk.sncl.key
        state = self._channels.get(key)
        samples = self._decode(chunk)
        if state is not None:
            expected = state.end_utc + timedelta(seconds=1.0 / state.sampling_rate_hz)
            gap = abs((chunk.start_time_utc - expected).total_seconds())
            if chunk.sampling_rate_hz != state.sampling_rate_hz or gap > 2.0:
                state = None
        if state is None:
            state = _Channel(
                sampling_rate_hz=chunk.sampling_rate_hz, start_utc=chunk.start_time_utc
            )
            self._channels[key] = state
        state.samples = np.concatenate([state.samples, samples])
        state.trace_ids.append(chunk.trace_id)
        cap = int(WINDOW_SECONDS * state.sampling_rate_hz * 1.5)
        if state.samples.size > cap:
            drop = state.samples.size - cap
            state.samples = state.samples[drop:]
            state.start_utc += timedelta(seconds=drop / state.sampling_rate_hz)

    def poll(self, stream_time_utc: datetime) -> list[DetectionCandidate]:
        """Score when enough has arrived; return at most one candidate."""
        needed = WINDOW_SECONDS if self.mode == "batch_600s" else SLIDING_MIN_SECONDS
        ready = [s for s in self._channels.values() if s.filled_seconds >= needed]
        if len({k for k, s in self._channels.items() if s.filled_seconds >= needed}) == 0:
            return []
        if (
            len(
                {
                    _station_key(_sncl_from_key(k))
                    for k, s in self._channels.items()
                    if s.filled_seconds >= needed
                }
            )
            < MIN_CONTRIBUTING_STATIONS
        ):
            return []
        if (
            self._last_scored is not None
            and self.mode == "sliding_180s"
            and (stream_time_utc - self._last_scored).total_seconds() < SLIDING_STRIDE_SECONDS
        ):
            return []
        if (
            self._last_fired is not None
            and (stream_time_utc - self._last_fired).total_seconds() < self.cooldown_seconds
        ):
            return []
        self._last_scored = stream_time_utc
        del ready

        waveform, valid, stations, trace_ids = self._assemble(stream_time_utc)
        if int(valid.any(axis=1).sum()) < MIN_CONTRIBUTING_STATIONS:
            return []
        self.windows_scored += 1

        features = np.array(
            [[compute_features(waveform, valid)[name] for name in FEATURE_NAMES]],
            dtype=np.float64,
        )
        class_probabilities = self.model.class_probabilities(features)[0]
        probability = float(self.model.calibrated_probability(features)[0])
        label = CLASSES[int(np.argmax(class_probabilities))]
        if probability < self.threshold:
            return []

        self._last_fired = stream_time_utc
        window_end = stream_time_utc
        window_start = window_end - timedelta(seconds=WINDOW_SECONDS)
        best = max(stations, key=lambda s: s[1]) if stations else None
        if best is None:
            return []
        return [
            DetectionCandidate(
                detection_id=f"{DETECTOR_NAME}/{self.mode}/{window_end.isoformat()}",
                sncl=best[0],
                detector=DETECTOR_NAME,
                detector_version=STREAMING_VERSION,
                window_start_utc=window_start,
                window_end_utc=window_end,
                detected_at_stream_utc=window_end,
                score=float(class_probabilities[0]),
                threshold=self.threshold,
                features={
                    "n_valid_channels": float(valid.sum()),
                    "n_contributing_receivers": float(valid.any(axis=1).sum()),
                },
                source_location=None,  # M1 does not locate; that is M2's job.
                sncls=[sncl for sncl, _ in stations],
                contributing_stations=[
                    ContributingStation(
                        sncl=sncl,
                        station_score=score,
                        components_used=list(COMPONENTS),
                    )
                    for sncl, score in stations
                ],
                probability=probability,
                probability_calibration=(
                    f"{self.model.calibrator.method} fitted on the validation fold only "
                    f"(n={self.model.calibrator.n_fitted})"
                ),
                class_label=label,  # type: ignore[arg-type]
                class_probabilities={
                    name: float(value)
                    for name, value in zip(CLASSES, class_probabilities, strict=True)
                },
                model_sha256=self.model.artifact.model_sha256,
                is_stub=False,
                input_trace_ids=trace_ids,
                notes=(
                    f"mode={self.mode}; response removed to velocity; "
                    f"{BANDPASS_HZ[0]}-{BANDPASS_HZ[1]} Hz at {TARGET_SAMPLING_RATE_HZ} Hz. "
                    "No location: M1 classifies, it does not locate."
                ),
            )
        ]

    # --- internals ----------------------------------------------------------------------

    def _decode(self, chunk: SeismicTrace) -> np.ndarray:
        if chunk.encoding == TraceEncoding.float32le:
            return np.frombuffer(chunk.data, dtype="<f4").astype(np.float64)
        from serac.adapters.seismic.obspy_codec import chunk_to_trace

        return np.asarray(chunk_to_trace(chunk).data, dtype=np.float64)

    def _assemble(
        self, stream_time_utc: datetime
    ) -> tuple[np.ndarray, np.ndarray, list[tuple[Sncl, float]], list[str]]:
        """Response-remove, filter, resample and lay the buffers out on the station axis."""
        from obspy import Stream, Trace, UTCDateTime

        waveform = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32)
        valid = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
        by_station: dict[str, dict[str, tuple[Sncl, _Channel]]] = defaultdict(dict)
        for key, state in self._channels.items():
            sncl = _sncl_from_key(key)
            component = _component_of(sncl.channel)
            if component is not None:
                by_station[_station_key(sncl)][component] = (sncl, state)

        end = UTCDateTime(stream_time_utc.timestamp())
        start = end - WINDOW_SECONDS
        stations: list[tuple[Sncl, float]] = []
        trace_ids: list[str] = []
        for slot, station in enumerate(sorted(by_station)):
            if slot >= MAX_STATIONS_PER_EVENT:
                break
            best_sncl: Sncl | None = None
            for index, component in enumerate(COMPONENTS):
                entry = by_station[station].get(component)
                if entry is None:
                    continue
                sncl, state = entry
                trace = Trace(
                    data=np.asarray(state.samples, dtype=np.float64),
                    header={
                        "network": sncl.network,
                        "station": sncl.station,
                        "location": sncl.location,
                        "channel": sncl.channel,
                        "sampling_rate": state.sampling_rate_hz,
                        "starttime": UTCDateTime(state.start_utc.timestamp()),
                    },
                )
                try:
                    processed = self._process(Stream([trace]), start, end)
                except ResponseRequiredError:
                    raise
                except Exception:
                    continue
                if processed is None:
                    continue
                waveform[slot, index] = processed
                valid[slot, index] = True
                trace_ids.extend(state.trace_ids[-4:])
                if index == 0 or best_sncl is None:
                    best_sncl = sncl
            if best_sncl is not None and valid[slot].any():
                stations.append((best_sncl, float(np.abs(waveform[slot]).max())))
        return waveform, valid, stations, trace_ids[:64]

    def _process(self, stream: Stream, start: object, end: object) -> np.ndarray | None:
        trace = stream[0].copy()
        trace.detrend("demean")
        trace.detrend("linear")
        trace.taper(0.05, type="cosine")
        if self.inventory is None:
            if self.require_response:
                raise ResponseRequiredError(
                    "no inventory: refusing to score raw counts as velocity"
                )
        else:
            try:
                trace.remove_response(inventory=self.inventory, output="VEL", pre_filt=PRE_FILT)
            except Exception as exc:
                if self.require_response:
                    raise ResponseRequiredError(
                        f"{trace.id}: no usable instrument response ({exc}); "
                        "raw counts are never scored as velocity"
                    ) from exc
                return None
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
        data = np.asarray(trace.data, dtype=np.float64)
        if data.size < N_SAMPLES:
            # sliding_180s pads the unseen tail with zeros: the model was trained on 600 s and
            # is being asked about less, which is exactly what the latency report measures.
            data = np.pad(data, (0, N_SAMPLES - data.size))
        sliced = data[:N_SAMPLES]
        if not np.all(np.isfinite(sliced)) or float(np.abs(sliced).max()) == 0.0:
            return None
        return sliced.astype(np.float32)


def _sncl_from_key(key: str) -> Sncl:
    network, station, location, channel = key.split(".")
    return Sncl(network=network, station=station, location=location, channel=channel)
