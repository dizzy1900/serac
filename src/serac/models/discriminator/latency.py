"""Detection latency, measured honestly in two modes, against a budget it cannot meet.

The brief sets a 60 s budget from origin to detection. **This architecture cannot reach it,
and the reason is physics rather than engineering.** Three terms add up:

1. **Travel time.** The nearest receiver is at least 100 km away by construction (closer
   stations are excluded so the window is a regional record, not a near-field one). A surface
   wave at 3 km/s needs ~33 s to arrive at 100 km and ~500 s at 1500 km.
2. **The band.** The discriminating energy is at 20-100 s period. A single 100 s cycle takes
   100 s to arrive. No filter can resolve a 100 s period from less than 100 s of record; that
   is a statement about Fourier analysis, not about implementation.
3. **The window.** The model was trained on 600 s windows from origin-60 s, so `batch_600s`
   cannot decide before 540 s after origin.

`sliding_180s` is the causal alternative: score as soon as 180 s of record exist, zero-padding
the unseen tail to the 12000 samples the model expects. It is faster and it is being asked a
question it was not trained on — the model has never seen a truncated window — so its scores
are reported alongside the batch scores rather than instead of them.

Two clocks are measured and never conflated. **Stream-time latency** is origin to the first
detection in the data's own time; it is what a deployment would experience once the data
arrive. **Compute latency** is the wall-clock cost of the scoring itself, which is what more
hardware could reduce. Only the second is an engineering number.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator.streaming import (
    SLIDING_MIN_SECONDS,
    WINDOW_SECONDS,
    DiscriminatorDetector,
    Mode,
)
from serac.models.discriminator.windows import MIN_DISTANCE_KM

LATENCY_VERSION = "0.1.0"

CHUNK_SECONDS: Final = 5.0
BRIEF_BUDGET_S: Final = 60.0

# Rayleigh-wave group velocity used only to state the travel-time floor. It is a round number
# for a statement about orders of magnitude, not a measured velocity for any path.
ASSUMED_SURFACE_WAVE_KM_S: Final = 3.0


class LatencyError(SeracError):
    """A latency run could not be made."""


class ModeLatency(BaseModel):
    """One mode's result on one event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    fired: bool
    first_detection_utc: AwareDatetime | None = None
    stream_latency_s: float | None = Field(
        default=None, description="Origin to first detection, in the data's own time."
    )
    compute_seconds_total: float = Field(ge=0)
    compute_seconds_per_poll_p50: float = Field(ge=0)
    compute_seconds_per_poll_p95: float = Field(ge=0)
    polls: int = Field(ge=0)
    windows_scored: int = Field(ge=0)
    chunks_ingested: int = Field(ge=0)
    probability: float | None = None
    class_label: str | None = None
    theoretical_floor_s: float
    meets_brief_budget: bool
    notes: list[str] = Field(default_factory=list)


class LatencyReport(BaseModel):
    """Both modes on one event, with the budget verdict stated plainly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latency_version: str = LATENCY_VERSION
    measured_at_utc: AwareDatetime
    event_id: str
    origin_utc: AwareDatetime
    n_receivers: int = Field(ge=0)
    modes: list[ModeLatency]
    brief_budget_s: float = BRIEF_BUDGET_S
    budget_met: bool
    verdict: str
    notes: list[str] = Field(default_factory=list)


def theoretical_floor_s(mode: Mode) -> float:
    """The earliest any correct implementation of this mode could decide, after origin.

    Travel time to the nearest permitted receiver, plus the record length the mode needs.
    Stated so the measured number can be read against a floor rather than against zero.
    """
    travel = MIN_DISTANCE_KM / ASSUMED_SURFACE_WAVE_KM_S
    record = WINDOW_SECONDS if mode == "batch_600s" else SLIDING_MIN_SECONDS
    return travel + record - 60.0  # windows start 60 s before origin


def measure(
    detector: DiscriminatorDetector,
    chunks: list[Any],
    origin_utc: datetime,
    *,
    mode: Mode,
) -> ModeLatency:
    """Drive a detector through a chunk stream in stream-time order and time both clocks.

    Chunks are fed at `--speed max`: wall-clock here measures computation, never transport.
    Stream time comes from the chunks' own timestamps, so it is meaningful at any speed.
    """
    detector.reset()
    ordered = sorted(chunks, key=lambda c: (c.start_time_utc, c.sncl.key))
    durations: list[float] = []
    first: datetime | None = None
    probability: float | None = None
    label: str | None = None
    polls = 0
    started = time.perf_counter()
    stream_time = origin_utc

    for chunk in ordered:
        detector.ingest(chunk)
        stream_time = max(stream_time, chunk.end_time_utc)
        began = time.perf_counter()
        candidates = detector.poll(stream_time)
        durations.append(time.perf_counter() - began)
        polls += 1
        if candidates and first is None:
            first = candidates[0].detected_at_stream_utc
            probability = candidates[0].probability
            label = candidates[0].class_label
            break
    total = time.perf_counter() - started

    ordered_durations = sorted(durations) or [0.0]
    p50 = ordered_durations[len(ordered_durations) // 2]
    p95 = ordered_durations[min(len(ordered_durations) - 1, int(0.95 * len(ordered_durations)))]
    floor = theoretical_floor_s(mode)
    stream_latency = (first - origin_utc).total_seconds() if first is not None else None
    return ModeLatency(
        mode=mode,
        fired=first is not None,
        first_detection_utc=first,
        stream_latency_s=stream_latency,
        compute_seconds_total=total,
        compute_seconds_per_poll_p50=p50,
        compute_seconds_per_poll_p95=p95,
        polls=polls,
        windows_scored=detector.windows_scored,
        chunks_ingested=detector.chunks_seen,
        probability=probability,
        class_label=label,
        theoretical_floor_s=floor,
        meets_brief_budget=bool(stream_latency is not None and stream_latency <= BRIEF_BUDGET_S),
        notes=[
            f"theoretical floor {floor:.0f} s = {MIN_DISTANCE_KM:.0f} km / "
            f"{ASSUMED_SURFACE_WAVE_KM_S:.0f} km/s travel + "
            f"{WINDOW_SECONDS if mode == 'batch_600s' else SLIDING_MIN_SECONDS:.0f} s record "
            "- 60 s pre-origin lead-in",
            "wall-clock here is computation only: chunks are fed at max speed, so no "
            "transport, acquisition or telemetry delay is included",
        ],
    )


def build_report(
    event_id: str,
    origin_utc: datetime,
    results: list[ModeLatency],
    *,
    n_receivers: int,
    notes: list[str] | None = None,
) -> LatencyReport:
    budget_met = any(r.meets_brief_budget for r in results)
    fastest = min(
        (r.stream_latency_s for r in results if r.stream_latency_s is not None), default=None
    )
    if budget_met:
        verdict = f"the {BRIEF_BUDGET_S:.0f} s budget was met"
    elif fastest is not None:
        verdict = (
            f"The brief's {BRIEF_BUDGET_S:.0f} s budget is NOT met and is not reachable for "
            f"this architecture. The fastest mode fired {fastest:.0f} s after origin against a "
            f"theoretical floor of {min(r.theoretical_floor_s for r in results):.0f} s. The "
            "floor is set by travel time to a >=100 km receiver plus the record length a "
            "20-100 s band requires; no amount of compute moves it. Reaching 60 s would need "
            "receivers inside 100 km and a shorter-period discriminant, which is a different "
            "component with different physics, not a faster version of this one."
        )
    else:
        verdict = (
            "No mode fired on this event, so no latency was measured. This is reported as "
            "the result; it is not a budget pass."
        )
    return LatencyReport(
        measured_at_utc=datetime.now(tz=UTC),
        event_id=event_id,
        origin_utc=origin_utc,
        n_receivers=n_receivers,
        modes=results,
        budget_met=budget_met,
        verdict=verdict,
        notes=notes or [],
    )


def chunk_stream_from_miniseed(
    path: Path,
    origin_utc: datetime,
    *,
    chunk_seconds: float = CHUNK_SECONDS,
    pre_origin_s: float = 60.0,
    post_origin_s: float = 540.0,
) -> list[Any]:
    """Slice a raw MiniSEED file into `SeismicTrace` chunks, as the replay source does.

    The bytes are the ones the dataset build fetched and ledgered: raw counts, not the
    response-removed velocity in the Zarr store. Feeding counts is deliberate — the detector's
    own response removal is part of what is being timed.
    """
    import hashlib

    import numpy as np
    from obspy import UTCDateTime, read

    from serac.domain.seismic import (
        SeismicTrace,
        Sncl,
        TraceEncoding,
        TraceProvenance,
        TraceSource,
    )

    stream = read(str(path), format="MSEED")
    start = UTCDateTime(origin_utc.timestamp()) - pre_origin_s
    end = UTCDateTime(origin_utc.timestamp()) + post_origin_s
    stream.trim(start, end)
    chunks: list[Any] = []
    retrieved = datetime.now(tz=UTC)
    for trace in stream:
        rate = float(trace.stats.sampling_rate)
        step = max(1, int(chunk_seconds * rate))
        data = np.asarray(trace.data, dtype="<f4")
        base = datetime.fromtimestamp(float(trace.stats.starttime.timestamp), tz=UTC)
        for index, offset in enumerate(range(0, data.size - step + 1, step)):
            payload = data[offset : offset + step].tobytes()
            chunk_start = base + timedelta(seconds=offset / rate)
            chunks.append(
                SeismicTrace(
                    trace_id=f"{trace.id}/{index}",
                    sncl=Sncl(
                        network=str(trace.stats.network),
                        station=str(trace.stats.station),
                        location=str(trace.stats.location or ""),
                        channel=str(trace.stats.channel),
                    ),
                    start_time_utc=chunk_start,
                    end_time_utc=chunk_start + timedelta(seconds=(step - 1) / rate),
                    sampling_rate_hz=rate,
                    npts=step,
                    encoding=TraceEncoding.float32le,
                    data=payload,
                    data_sha256=hashlib.sha256(payload).hexdigest(),
                    sequence=index,
                    provenance=TraceProvenance(
                        # `fdsn`, not `fixture`: these bytes came from an fdsnws-dataselect
                        # request made by the dataset build and live under data/raw, not in
                        # the committed fixture tree.
                        source=TraceSource.fdsn,
                        retrieved_at=retrieved,
                        notes=f"replayed from {path.as_posix()} (ledgered by the M1 build)",
                    ),
                )
            )
    return chunks
