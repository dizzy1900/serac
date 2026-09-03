"""Replay fixtures and the replay latency report.

`FixtureManifest` is the sidecar `manifest.json` written next to every set of committed
waveform fixtures under `data/fixtures/seismic/<event>/`. `ReplayReport` is the public shape
of `reports/replay/<event>.json`, exported as `contracts/replay-report.v0.json`.

Latency semantics: *stream-time* latencies are differences between the `stream_time_utc` of
messages and are meaningful at any replay speed. *Wall-clock* latencies compare
`produced_at_utc` values and are only comparable to a live deployment at speed 1.0, so the
report carries a `valid` flag next to them rather than letting a `--speed max` run look fast.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

REPLAY_CONTRACT_VERSION = "0.1.0"
FIXTURE_MANIFEST_CONTRACT_VERSION = "0.1.0"

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_utc: AwareDatetime
    end_utc: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc")
        return self


class FixtureFile(BaseModel):
    """One committed file inside a fixture directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, description="Path relative to the fixture directory.")
    kind: Literal["miniseed", "stationxml", "other"]
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    sncl: str | None = Field(default=None, description="`NET.STA.LOC.CHA` for waveform files.")
    start_utc: AwareDatetime | None = None
    end_utc: AwareDatetime | None = None
    sampling_rate_hz: float | None = Field(default=None, gt=0)
    npts: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, description="Request URL the bytes were fetched from.")


class FixtureRequest(BaseModel):
    """How the fixture was requested, enough to reproduce it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client: str = Field(min_length=1, description="FDSN client alias used, e.g. EARTHSCOPE.")
    base_url: str = Field(min_length=1, description="Resolved service base URL, never an alias.")
    bulk: list[list[str]] = Field(
        default_factory=list, description="fdsnws bulk rows: net sta loc cha start end."
    )
    station_level: str | None = None
    tool: str | None = Field(default=None, description="e.g. `obspy 1.5.1`.")


class FixtureManifest(BaseModel):
    """Sidecar manifest for a replay fixture directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = FIXTURE_MANIFEST_CONTRACT_VERSION
    event_id: str = Field(min_length=1)
    window: TimeWindow
    files: list[FixtureFile] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list, description="SNCLs requested but not returned by the service."
    )
    request: FixtureRequest
    retrieved_at_utc: AwareDatetime | None = None
    licence: str | None = Field(
        default=None, description="Licence statement as read at the data centre, else null."
    )
    licence_source_url: str | None = None
    status: Literal["fetched", "partial", "not_fetched"]
    notes: str | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> Self:
        if self.status == "not_fetched" and self.files:
            raise ValueError("not_fetched manifests list no files")
        if self.status in ("fetched", "partial") and (
            not self.files or self.retrieved_at_utc is None
        ):
            raise ValueError("fetched/partial manifests need files and retrieved_at_utc")
        if self.status == "partial" and not self.missing:
            raise ValueError("partial manifests must list what is missing")
        return self


class MessageMark(BaseModel):
    """When and where a first-of-kind message appeared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    stream_time_utc: AwareDatetime
    produced_at_utc: AwareDatetime
    sncl: str | None = None
    score: float | None = None


class StreamTimeLatencies(BaseModel):
    """Seconds between stream times; always meaningful."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_to_first_detection_s: float | None = None
    origin_to_first_cap_s: float | None = None
    first_detection_to_first_cap_s: float | None = None


class WallClockLatencies(BaseModel):
    """Seconds between `produced_at_utc` values; only comparable to live at speed 1.0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool = Field(description="True only when the replay ran at speed 1.0.")
    first_chunk_to_first_detection_s: float | None = None
    first_detection_to_first_cap_s: float | None = None
    total_run_s: float | None = Field(default=None, ge=0)


class DetectorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)
    is_stub: bool = True


class ReplayCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunks_published: int = Field(ge=0)
    chunks_consumed: int = Field(ge=0)
    detections_emitted: int = Field(ge=0)
    cap_messages_emitted: int = Field(ge=0)
    pending_after_drain: int = Field(ge=0)


class FixtureRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    provenance: Literal["real", "synthetic"]


class ReplayReport(BaseModel):
    """`reports/replay/<event>.json`: what a replay run did and how long each hop took."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = REPLAY_CONTRACT_VERSION
    replay_run_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    bus: Literal["in_memory", "redis"]
    speed: float | Literal["max"]
    status: Literal["completed", "failed", "no_fixture"]
    fixtures: list[FixtureRef] = Field(default_factory=list)
    contains_synthetic: bool = False
    origin_time_utc: AwareDatetime | None = None
    origin_time_source: str | None = Field(
        default=None, description="Event-library record id the origin time was read from."
    )
    window: TimeWindow | None = None
    counts: ReplayCounts
    first_detection: MessageMark | None = None
    first_cap: MessageMark | None = None
    stream_time_latencies: StreamTimeLatencies
    wall_clock_latencies: WallClockLatencies
    detector: DetectorSummary
    is_stub: Literal[True] = True
    started_at_utc: AwareDatetime
    finished_at_utc: AwareDatetime
    caveats: list[str] = Field(min_length=1)
    error: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError("finished_at_utc must not precede started_at_utc")
        if self.wall_clock_latencies.valid and self.speed != 1.0:
            raise ValueError("wall-clock latencies are only valid at speed 1.0")
        if self.contains_synthetic != any(f.provenance == "synthetic" for f in self.fixtures):
            raise ValueError("contains_synthetic must reflect the fixtures list")
        if self.status == "failed" and not self.error:
            raise ValueError("failed reports must carry an error")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {
    "replay-report": ReplayReport,
    "fixture-manifest": FixtureManifest,
}
