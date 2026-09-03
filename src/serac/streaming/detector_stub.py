"""STUB — replaced in Prompt 2.

A placeholder long-period / short-period energy-ratio filter that stands in for the real
single-force discriminator. It exists to prove that `SeismicTrace` chunks flow through the
bus into `DetectionCandidate`s and on to CAP; it has **no** validated detection performance,
no discriminator, and never emits a source location (`DetectionCandidate.source_location` is
typed `None`).

What it computes, per SNCL: a 120 s ring buffer of samples is Hann-windowed and transformed
with `numpy.fft.rfft`; the summed power in 0.02-0.1 Hz (long period) is divided by the summed
power in 1-10 Hz (short period, clipped at Nyquist). When the ratio exceeds `threshold` and
the channel is not in cooldown, one candidate is emitted. `threshold = 10.0` is a stated
placeholder: it was **not** tuned on any event, and whether it fires on the committed Chamoli
or Langtang fixtures is an observation the replay report records, not a target. Synthetic
chunks are refused unless `allow_synthetic` is set, so a plumbing test cannot masquerade as an
observation-driven detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from serac.adapters.seismic.obspy_codec import chunk_to_trace
from serac.domain import topics
from serac.domain.codec import wrap
from serac.domain.detection import DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.seismic import SeismicTrace, Sncl, TraceEncoding
from serac.errors import SeracError
from serac.ports.bus import Received
from serac.streaming.stage import Stage

STUB_MARKER = "STUB — replaced in Prompt 2."
DETECTOR_NAME = "lp-sp-ratio-stub"
DETECTOR_VERSION = "0.1.0"


class DetectorStubError(SeracError):
    """The stub refused an input."""


class SyntheticInputRefusedError(DetectorStubError):
    """A synthetic chunk reached the detector without `allow_synthetic`."""


class DetectorStubConfig(BaseModel):
    """Parameters of the placeholder; every value is a stated assumption, none is tuned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    buffer_seconds: float = Field(default=120.0, gt=0)
    min_fill_seconds: float = Field(
        default=60.0, gt=0, description="No ratio is computed before this much data is buffered."
    )
    lp_band_hz: tuple[float, float] = (0.02, 0.1)
    sp_band_hz: tuple[float, float] = (1.0, 10.0)
    threshold: float = Field(
        default=10.0, gt=0, description="PLACEHOLDER: untuned; not a calibrated trigger level."
    )
    cooldown_seconds: float = Field(default=300.0, ge=0)
    gap_tolerance_samples: float = Field(
        default=1.5, gt=0, description="A jump larger than this many samples resets the buffer."
    )
    allow_synthetic: bool = False

    def as_params(self) -> dict[str, float | int | str | bool]:
        return {
            "buffer_seconds": self.buffer_seconds,
            "min_fill_seconds": self.min_fill_seconds,
            "lp_band_hz": f"{self.lp_band_hz[0]}-{self.lp_band_hz[1]}",
            "sp_band_hz": f"{self.sp_band_hz[0]}-{self.sp_band_hz[1]}",
            "threshold": self.threshold,
            "threshold_is_placeholder": True,
            "cooldown_seconds": self.cooldown_seconds,
            "gap_tolerance_samples": self.gap_tolerance_samples,
            "allow_synthetic": self.allow_synthetic,
        }


@dataclass(frozen=True)
class RatioSample:
    """One computed ratio, kept for the replay report and the golden test."""

    sncl: str
    window_start_utc: datetime
    window_end_utc: datetime
    n_samples: int
    sampling_rate_hz: float
    lp_energy: float
    sp_energy: float
    ratio: float
    fired: bool


@dataclass
class _ChannelState:
    sampling_rate_hz: float
    start_utc: datetime
    samples: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    trace_ids: list[str] = field(default_factory=list)
    last_fired_utc: datetime | None = None
    gaps: int = 0

    @property
    def end_utc(self) -> datetime:
        if self.samples.size == 0:
            return self.start_utc
        return self.start_utc + timedelta(seconds=(self.samples.size - 1) / self.sampling_rate_hz)


def band_energy(freqs: np.ndarray, power: np.ndarray, band: tuple[float, float]) -> float:
    lo, hi = band
    mask = (freqs >= lo) & (freqs <= hi)
    return float(power[mask].sum())


def lp_sp_ratio(
    samples: np.ndarray,
    sampling_rate_hz: float,
    *,
    lp_band: tuple[float, float],
    sp_band: tuple[float, float],
) -> tuple[float, float, float]:
    """(lp_energy, sp_energy, ratio) of a Hann-windowed, demeaned window."""
    x = np.asarray(samples, dtype=np.float64)
    x = x - x.mean()
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    power = np.abs(spectrum) ** 2
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sampling_rate_hz)
    lp = band_energy(freqs, power, lp_band)
    sp = band_energy(freqs, power, sp_band)
    ratio = lp / sp if sp > 0 else float("inf") if lp > 0 else 0.0
    return lp, sp, ratio


def decode_samples(chunk: SeismicTrace) -> np.ndarray:
    if chunk.encoding == TraceEncoding.float32le:
        return np.frombuffer(chunk.data, dtype="<f4").astype(np.float64)
    trace = chunk_to_trace(chunk)
    return np.asarray(trace.data, dtype=np.float64)


class DetectorStub(Stage):
    """`serac.waveforms` -> `serac.detections`. See the module docstring: this is a stub."""

    name = "detector-stub"
    input_topic = topics.WAVEFORMS
    group = "detector"

    def __init__(self, config: DetectorStubConfig | None = None) -> None:
        self.config = config or DetectorStubConfig()
        self._channels: dict[str, _ChannelState] = {}
        self.history: list[RatioSample] = []
        self.detections = 0
        self.chunks_seen = 0
        self.gap_resets = 0

    # --- buffer management ----------------------------------------------------------------

    def _append(self, chunk: SeismicTrace, samples: np.ndarray) -> _ChannelState:
        key = chunk.sncl.key
        state = self._channels.get(key)
        if state is not None:
            expected = state.end_utc + timedelta(seconds=1.0 / state.sampling_rate_hz)
            jump_samples = abs(
                (chunk.start_time_utc - expected).total_seconds() * state.sampling_rate_hz
            )
            if (
                chunk.sampling_rate_hz != state.sampling_rate_hz
                or jump_samples > self.config.gap_tolerance_samples
            ):
                state = None
                self.gap_resets += 1
        if state is None:
            state = _ChannelState(
                sampling_rate_hz=chunk.sampling_rate_hz, start_utc=chunk.start_time_utc
            )
            self._channels[key] = state
        state.samples = np.concatenate([state.samples, samples])
        state.trace_ids.append(chunk.trace_id)
        max_samples = round(self.config.buffer_seconds * state.sampling_rate_hz)
        if state.samples.size > max_samples:
            drop = state.samples.size - max_samples
            state.samples = state.samples[drop:]
            state.start_utc = state.start_utc + timedelta(seconds=drop / state.sampling_rate_hz)
            # keep only the ids that can still overlap the buffer
            keep = max(1, int(np.ceil(max_samples / max(samples.size, 1))) + 1)
            state.trace_ids = state.trace_ids[-keep:]
        return state

    # --- stage ------------------------------------------------------------------------------

    def evaluate(self, chunk: SeismicTrace) -> DetectionCandidate | None:
        """Ingest one chunk; return a candidate when the placeholder fires."""
        if chunk.is_synthetic and not self.config.allow_synthetic:
            raise SyntheticInputRefusedError(
                f"chunk {chunk.trace_id} is synthetic; DetectorStub(allow_synthetic=True) "
                "is required to process it"
            )
        self.chunks_seen += 1
        state = self._append(chunk, decode_samples(chunk))
        filled_s = state.samples.size / state.sampling_rate_hz
        if filled_s < self.config.min_fill_seconds:
            return None
        lp, sp, ratio = lp_sp_ratio(
            state.samples,
            state.sampling_rate_hz,
            lp_band=self.config.lp_band_hz,
            sp_band=self.config.sp_band_hz,
        )
        in_cooldown = (
            state.last_fired_utc is not None
            and (state.end_utc - state.last_fired_utc).total_seconds()
            < self.config.cooldown_seconds
        )
        fired = bool(np.isfinite(ratio) and ratio > self.config.threshold and not in_cooldown)
        self.history.append(
            RatioSample(
                sncl=chunk.sncl.key,
                window_start_utc=state.start_utc,
                window_end_utc=state.end_utc,
                n_samples=int(state.samples.size),
                sampling_rate_hz=state.sampling_rate_hz,
                lp_energy=lp,
                sp_energy=sp,
                ratio=ratio,
                fired=fired,
            )
        )
        if not fired:
            return None
        state.last_fired_utc = state.end_utc
        self.detections += 1
        nyquist = state.sampling_rate_hz / 2
        return DetectionCandidate(
            detection_id=f"{DETECTOR_NAME}/{chunk.sncl.key}/{state.end_utc.isoformat()}",
            sncl=Sncl.model_validate(chunk.sncl.model_dump()),
            detector=DETECTOR_NAME,
            detector_version=DETECTOR_VERSION,
            window_start_utc=state.start_utc,
            window_end_utc=state.end_utc,
            detected_at_stream_utc=state.end_utc,
            score=ratio,
            threshold=self.config.threshold,
            features={
                "lp_energy": lp,
                "sp_energy": sp,
                "n_samples": float(state.samples.size),
                "sampling_rate_hz": state.sampling_rate_hz,
                "sp_band_upper_effective_hz": min(self.config.sp_band_hz[1], nyquist),
            },
            source_location=None,
            is_stub=True,
            input_trace_ids=list(state.trace_ids),
            notes=(
                f"{STUB_MARKER} Placeholder LP/SP energy ratio with an untuned threshold; "
                "no discriminator, no location, no validated performance."
            ),
        )

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        chunk = received.envelope.payload
        if not isinstance(chunk, SeismicTrace):
            raise DetectorStubError(
                f"expected SeismicTrace on {self.input_topic}, got {type(chunk).__name__}"
            )
        candidate = self.evaluate(chunk)
        if candidate is None:
            return []
        return [
            wrap(
                candidate,
                topic=topics.DETECTIONS,
                producer=self.name,
                stream_time_utc=candidate.detected_at_stream_utc,
                causation_id=received.envelope.message_id,
                replay_run_id=received.envelope.replay_run_id,
            )
        ]
