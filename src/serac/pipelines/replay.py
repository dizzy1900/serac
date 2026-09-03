"""`serac replay`: stream an archived event window through the real-time lane and time it.

Chunks come from a hash-verified fixture (`data/fixtures/seismic/<event>/`), from FDSN when
`--online` is given and no fixture exists, or from the in-code synthetic lane for
`synthetic-lp-burst`. They are published on `serac.waveforms` in stream-time order, paced by a
`Clock` at `--speed 1.0` (so a `VirtualClock` can prove the sleep schedule) or as fast as
possible at `--speed max`. After every chunk the in-process pipeline (detector stub, CAP stub)
is drained, so the run is deterministic on the in-memory bus.

The origin time comes from the event-library record `data/events/<id>.json`
(`MassMovementEvent.time.datetime_utc`), never from a constant here; when the record is
absent every origin-relative latency is `null`. Wall-clock latencies are flagged valid only at
speed 1.0. The report is `reports/replay/<event>.json` (`ReplayReport`), and its caveats say
in every case that the detector is a stub and the figures prove plumbing, not latency.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from serac.adapters.bus.in_memory import InMemoryBus
from serac.adapters.seismic.fdsn import haversine_km
from serac.domain import topics
from serac.domain.codec import wrap
from serac.domain.envelope import Envelope
from serac.domain.events import MassMovementEvent
from serac.domain.replay import (
    DetectorSummary,
    MessageMark,
    ReplayCounts,
    ReplayReport,
    ReplayStation,
    StreamTimeLatencies,
    WallClockLatencies,
)
from serac.errors import SeracError
from serac.ports.bus import MessageBus, Received
from serac.ports.clock import Clock, WallClock
from serac.streaming.cap_stub import CapStub
from serac.streaming.detector_stub import (
    DETECTOR_NAME,
    DETECTOR_VERSION,
    STUB_MARKER,
    DetectorStub,
    DetectorStubConfig,
)
from serac.streaming.pipeline import Pipeline
from serac.streaming.replay_source import (
    SYNTHETIC_EVENT_ID,
    FixtureNotFetchedError,
    FixtureReplaySource,
    ReplaySource,
    StationInfo,
    SyntheticReplaySource,
    fixture_dir_for,
)
from serac.streaming.stage import Stage

Speed = float | Literal["max"]
BusKind = Literal["in_memory", "redis"]

PRODUCER = "replay"
REPORT_SUBDIR = Path("reports") / "replay"

# Fixture directories are named after the seismic window; event-library records may use a
# longer slug. Only aliases the event library actually uses belong here.
EVENT_RECORD_ALIASES: dict[str, tuple[str, ...]] = {
    "langtang-2026": ("langtang-2026", "langtang-lhende-2026"),
}

BASE_CAVEATS = (
    f"{STUB_MARKER} The detector is a placeholder LP/SP energy-ratio filter with an untuned "
    "threshold; whether it fires is an observation, not a validated detection.",
    "Latency figures prove plumbing only (messages traverse the bus and stages); they are not "
    "evidence for the 180 s design budget in docs/ARCHITECTURE.md.",
    "No source location is inferred anywhere in this lane; CAP messages are status=Test, "
    "scope=Private and carry no area element.",
)


class ReplayError(SeracError):
    """The replay could not run."""


def parse_speed(value: str) -> Speed:
    """`max` or a positive float."""
    if value.strip().lower() == "max":
        return "max"
    try:
        speed = float(value)
    except ValueError as exc:
        raise ReplayError(f"speed must be a positive number or 'max', got {value!r}") from exc
    if speed <= 0:
        raise ReplayError("speed must be positive")
    return speed


@dataclass(frozen=True)
class ReplayConfig:
    event_id: str
    speed: Speed = "max"
    chunk_seconds: float = 5.0
    bus: BusKind = "in_memory"
    report_dir: Path | None = None
    online: bool = False
    repo_root: Path = field(default_factory=Path.cwd)
    detector: DetectorStubConfig | None = None
    redis_url: str | None = None

    def __post_init__(self) -> None:
        if self.chunk_seconds <= 0:
            raise ReplayError("chunk_seconds must be positive")
        if isinstance(self.speed, float) and self.speed <= 0:
            raise ReplayError("speed must be positive")


@dataclass
class OriginInfo:
    """What the event library says, or nothing."""

    record_id: str | None = None
    origin_time_utc: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    error: str | None = None


def load_origin(repo_root: Path, event_id: str) -> OriginInfo:
    """Origin time and source location from `data/events/<id>.json`, else empty."""
    candidates = EVENT_RECORD_ALIASES.get(event_id, (event_id,))
    for record_id in candidates:
        path = repo_root / "data" / "events" / f"{record_id}.json"
        if not path.exists():
            continue
        try:
            record = MassMovementEvent.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            return OriginInfo(record_id=record_id, error=f"{path}: invalid event record: {exc}")
        return OriginInfo(
            record_id=record_id,
            origin_time_utc=record.time.datetime_utc,
            latitude=record.source_location.lat,
            longitude=record.source_location.lon,
        )
    return OriginInfo()


class _Recording(Stage):
    """Wraps a stage to remember the first envelope it published and count all of them."""

    def __init__(self, inner: Stage) -> None:
        self.inner = inner
        self.name = inner.name
        self.input_topic = inner.input_topic
        self.group = inner.group
        self.first: Envelope[BaseModel] | None = None
        self.emitted = 0
        self.consumed = 0

    def process(self, received: Received) -> list[Envelope[BaseModel]]:
        outputs = self.inner.process(received)
        self.consumed += 1
        self.emitted += len(outputs)
        if outputs and self.first is None:
            self.first = outputs[0]
        return outputs


def _mark(envelope: Envelope[BaseModel]) -> MessageMark:
    payload = envelope.payload
    sncl = getattr(getattr(payload, "sncl", None), "key", None)
    score = getattr(payload, "score", None)
    return MessageMark(
        message_id=envelope.message_id,
        stream_time_utc=envelope.stream_time_utc,
        produced_at_utc=envelope.produced_at_utc,
        sncl=sncl if isinstance(sncl, str) else None,
        score=float(score) if isinstance(score, int | float) else None,
    )


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds()


def resolve_source(
    config: ReplayConfig, *, fetch_online: Callable[[ReplayConfig], Path] | None = None
) -> ReplaySource:
    """Synthetic lane, else the fixture directory, else FDSN when `--online`, else refuse."""
    if config.event_id == SYNTHETIC_EVENT_ID:
        return SyntheticReplaySource()
    fixture_dir = fixture_dir_for(config.repo_root, config.event_id)
    if (fixture_dir / "manifest.json").exists():
        return FixtureReplaySource(fixture_dir, repo_root=config.repo_root)
    if config.online:
        if fetch_online is None:
            raise ReplayError("online fetch requested but no fetcher was provided")
        fetched_dir = fetch_online(config)
        return FixtureReplaySource(fetched_dir, repo_root=config.repo_root)
    raise FixtureNotFetchedError(
        f"no fixture for event {config.event_id!r} under {fixture_dir}; re-run with --online "
        "to fetch from FDSN (writes under data/raw/ with ledger rows)"
    )


def make_bus(config: ReplayConfig) -> MessageBus:
    if config.bus == "in_memory":
        return InMemoryBus()
    from serac.adapters.bus.redis_streams import RedisStreamsBus
    from serac.settings import get_settings

    url = config.redis_url or get_settings().serac_redis_url
    return RedisStreamsBus.from_url(url)


def _stations(
    source: ReplaySource, origin: OriginInfo, published_by_sncl: dict[str, int]
) -> list[ReplayStation]:
    out: list[ReplayStation] = []
    for info in source.stations():
        out.append(_station(info, origin, published_by_sncl.get(info.sncl, 0)))
    return out


def _station(info: StationInfo, origin: OriginInfo, published: int) -> ReplayStation:
    distance = None
    if (
        origin.latitude is not None
        and origin.longitude is not None
        and info.latitude is not None
        and info.longitude is not None
    ):
        distance = haversine_km(origin.latitude, origin.longitude, info.latitude, info.longitude)
    return ReplayStation(
        sncl=info.sncl,
        latitude=info.latitude,
        longitude=info.longitude,
        elevation_m=info.elevation_m,
        distance_from_source_km=distance,
        chunks_published=published,
    )


def run_replay(
    config: ReplayConfig,
    *,
    bus: MessageBus | None = None,
    clock: Clock | None = None,
    source: ReplaySource | None = None,
    fetch_online: Callable[[ReplayConfig], Path] | None = None,
    write_report: bool = True,
) -> ReplayReport:
    """Run the lane end to end and return (and by default write) the report."""
    clock = clock or WallClock()
    started_at = clock.now()
    run_id = uuid.uuid4().hex
    source = source or resolve_source(config, fetch_online=fetch_online)
    origin = load_origin(config.repo_root, config.event_id)
    if source.contains_synthetic and isinstance(source, SyntheticReplaySource):
        origin = OriginInfo(
            record_id="synthetic: burst_start_s parameter",
            origin_time_utc=source.origin_time_utc,
        )

    detector_config = config.detector or DetectorStubConfig(
        allow_synthetic=source.contains_synthetic
    )
    if source.contains_synthetic and not detector_config.allow_synthetic:
        detector_config = detector_config.model_copy(update={"allow_synthetic": True})
    detector = DetectorStub(detector_config)
    xsd_path = config.repo_root / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"
    cap = CapStub(xsd_path=xsd_path, clock=clock)
    rec_detector = _Recording(detector)
    rec_cap = _Recording(cap)

    owns_bus = bus is None
    bus = bus or make_bus(config)
    pipeline = Pipeline(bus, [rec_detector, rec_cap])

    caveats = list(BASE_CAVEATS) + source.caveats()
    if origin.error:
        caveats.append(origin.error)
    if origin.origin_time_utc is None:
        caveats.append(
            "No event-library record with an origin time was found; origin-relative latencies "
            "are null."
        )
    if config.speed != 1.0:
        caveats.append(
            f"Replay speed {config.speed}: wall-clock latencies are not comparable to live and "
            "are flagged invalid."
        )

    published = 0
    published_by_sncl: dict[str, int] = {}
    first_chunk: Envelope[BaseModel] | None = None
    first_stream_time: datetime | None = None
    pending_total = 0
    error: str | None = None
    status: Literal["completed", "failed"] = "completed"
    try:
        for chunk in source.chunks(chunk_seconds=config.chunk_seconds):
            if first_stream_time is None:
                first_stream_time = chunk.start_time_utc
            if config.speed != "max":
                offset_s = (chunk.start_time_utc - first_stream_time).total_seconds()
                target = started_at.__class__.fromtimestamp(
                    started_at.timestamp() + offset_s / float(config.speed), tz=UTC
                )
                clock.sleep_until(target)
            envelope: Envelope[BaseModel] = wrap(
                chunk,
                topic=topics.WAVEFORMS,
                producer=PRODUCER,
                stream_time_utc=chunk.start_time_utc,
                replay_run_id=run_id,
                produced_at_utc=clock.now(),
            )
            bus.publish(envelope)
            published += 1
            published_by_sncl[chunk.sncl.key] = published_by_sncl.get(chunk.sncl.key, 0) + 1
            if first_chunk is None:
                first_chunk = envelope
            drained = pipeline.drain()
            pending_total = drained.total_pending
        final = pipeline.drain()
        pending_total = final.total_pending
    except SeracError as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if owns_bus:
            bus.close()

    finished_at = clock.now()
    first_detection = _mark(rec_detector.first) if rec_detector.first else None
    first_cap = _mark(rec_cap.first) if rec_cap.first else None
    wall_valid = config.speed == 1.0 and status == "completed"
    first_det_lag = _seconds(
        first_detection.stream_time_utc if first_detection else None, origin.origin_time_utc
    )
    if first_det_lag is not None and first_det_lag < 0:
        caveats.append(
            f"The first candidate precedes the event origin by {abs(first_det_lag):.0f} s, so it "
            "cannot be a detection of this event: the placeholder threshold is crossed by "
            "background noise. Negative origin-relative latencies mean exactly that."
        )

    report = ReplayReport(
        replay_run_id=run_id,
        event_id=config.event_id,
        bus=config.bus,
        speed=config.speed,
        status=status,
        fixtures=source.fixture_refs(),
        contains_synthetic=source.contains_synthetic,
        origin_time_utc=origin.origin_time_utc,
        origin_time_source=origin.record_id,
        window=source.window(),
        chunk_seconds=config.chunk_seconds,
        stations=_stations(source, origin, published_by_sncl),
        counts=ReplayCounts(
            chunks_published=published,
            chunks_consumed=rec_detector.consumed,
            detections_emitted=rec_detector.emitted,
            cap_messages_emitted=rec_cap.emitted,
            pending_after_drain=pending_total,
        ),
        first_detection=first_detection,
        first_cap=first_cap,
        stream_time_latencies=StreamTimeLatencies(
            origin_to_first_detection_s=_seconds(
                first_detection.stream_time_utc if first_detection else None,
                origin.origin_time_utc,
            ),
            origin_to_first_cap_s=_seconds(
                first_cap.stream_time_utc if first_cap else None, origin.origin_time_utc
            ),
            first_detection_to_first_cap_s=_seconds(
                first_cap.stream_time_utc if first_cap else None,
                first_detection.stream_time_utc if first_detection else None,
            ),
        ),
        wall_clock_latencies=WallClockLatencies(
            valid=wall_valid,
            first_chunk_to_first_detection_s=_seconds(
                first_detection.produced_at_utc if first_detection else None,
                first_chunk.produced_at_utc if first_chunk else None,
            ),
            first_detection_to_first_cap_s=_seconds(
                first_cap.produced_at_utc if first_cap else None,
                first_detection.produced_at_utc if first_detection else None,
            ),
            total_run_s=max(0.0, (finished_at - started_at).total_seconds()),
        ),
        detector=DetectorSummary(
            name=DETECTOR_NAME,
            version=DETECTOR_VERSION,
            params=detector_config.as_params(),
            is_stub=True,
        ),
        is_stub=True,
        started_at_utc=started_at,
        finished_at_utc=finished_at,
        caveats=caveats,
        error=error,
    )
    if write_report:
        write_replay_report(report, config.report_dir or (config.repo_root / REPORT_SUBDIR))
    return report


def write_replay_report(report: ReplayReport, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{report.event_id}.json"
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_replay_report(path: Path) -> ReplayReport:
    return ReplayReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
