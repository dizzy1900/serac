"""Seismic waveform contracts carried on the message bus.

A `SeismicTrace` is one chunk of a single-channel time series (typically 5 s in replay, one
MiniSEED record from SeedLink). The waveform bytes travel opaquely: `encoding` says how to
decode them and `data_sha256` pins them. Only `serac.adapters.seismic.obspy_codec` knows how
to turn an ObsPy `Trace` into a chunk and back; this module imports nothing but stdlib and
pydantic.

Provenance is mandatory. A chunk whose `provenance.source` is `synthetic` must carry `notes`
explaining what generated it, so nothing synthetic can masquerade as an observation.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SEISMIC_CONTRACT_VERSION = "0.1.0"

SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SNCL_KEY = re.compile(r"^([A-Z0-9]{1,2})\.([A-Z0-9]{1,5})\.([A-Z0-9]{0,2})\.([A-Z0-9]{3})$")


class TraceSource(StrEnum):
    """Where the samples came from."""

    seedlink = "seedlink"  # live feed
    fdsn = "fdsn"  # fdsnws-dataselect archive request made in-process
    fixture = "fixture"  # bytes read from a committed `data/fixtures/seismic/<event>/` file
    synthetic = "synthetic"  # generated in code; never an observation


class TraceEncoding(StrEnum):
    """How `SeismicTrace.data` is laid out."""

    miniseed = "miniseed"  # one or more MiniSEED records (Steim2, 512-byte records preferred)
    float32le = "float32le"  # raw little-endian IEEE float32 samples, no header


class Sncl(BaseModel):
    """SEED network/station/location/channel identifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    network: str = Field(pattern=r"^[A-Z0-9]{1,2}$")
    station: str = Field(pattern=r"^[A-Z0-9]{1,5}$")
    location: str = Field(default="", pattern=r"^[A-Z0-9]{0,2}$")
    channel: str = Field(pattern=r"^[A-Z0-9]{3}$")

    @property
    def key(self) -> str:
        """Dotted form, e.g. `NK.KKN..BHZ`; an empty location keeps its dot."""
        return f"{self.network}.{self.station}.{self.location}.{self.channel}"

    @classmethod
    def from_key(cls, key: str) -> Sncl:
        """Parse `NET.STA.LOC.CHA` (location may be empty)."""
        match = _SNCL_KEY.match(key)
        if match is None:
            raise ValueError(f"not a NET.STA.LOC.CHA key: {key!r}")
        net, sta, loc, cha = match.groups()
        return cls(network=net, station=sta, location=loc, channel=cha)


class TraceProvenance(BaseModel):
    """Origin of a chunk's samples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: TraceSource
    server: str | None = Field(
        default=None,
        description="Resolved base URL or host:port the bytes came from; never an alias.",
    )
    retrieved_at: AwareDatetime | None = None
    licence: str | None = Field(
        default=None, description="Licence statement as read at the data centre, or null."
    )
    licence_source_url: str | None = None
    fixture_path: str | None = Field(
        default=None, description="Repo-relative path when `source` is `fixture`."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _synthetic_requires_notes(self) -> Self:
        if self.source == TraceSource.synthetic and not (self.notes and self.notes.strip()):
            raise ValueError("synthetic provenance requires non-empty notes")
        if self.source == TraceSource.fixture and self.fixture_path is None:
            raise ValueError("fixture provenance requires fixture_path")
        return self


class SeismicTrace(BaseModel):
    """One chunk of one channel, as published on `serac.waveforms`.

    Invariants enforced here:

    * `end_time_utc` equals `start_time_utc + (npts - 1) / sampling_rate_hz` within one sample;
    * `data_sha256` is the SHA-256 of `data`;
    * `float32le` payloads are exactly `4 * npts` bytes.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )

    contract_version: str = SEISMIC_CONTRACT_VERSION
    trace_id: str = Field(min_length=1)
    sncl: Sncl
    start_time_utc: AwareDatetime
    end_time_utc: AwareDatetime
    sampling_rate_hz: float = Field(gt=0)
    npts: int = Field(ge=1)
    encoding: TraceEncoding
    data: bytes = Field(min_length=1)
    data_sha256: str = Field(pattern=SHA256_PATTERN)
    units: Literal["counts"] = "counts"
    quality: str | None = Field(
        default=None, pattern=r"^[DRQM]$", description="MiniSEED data quality indicator."
    )
    sequence: int = Field(ge=0, description="Chunk index within its stream, in stream-time order.")
    provenance: TraceProvenance

    @property
    def duration_s(self) -> float:
        return (self.npts - 1) / self.sampling_rate_hz

    @property
    def is_synthetic(self) -> bool:
        return self.provenance.source == TraceSource.synthetic

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        expected_end = self.start_time_utc + timedelta(seconds=self.duration_s)
        tolerance = timedelta(seconds=1.0 / self.sampling_rate_hz)
        if abs(self.end_time_utc - expected_end) > tolerance:
            raise ValueError(
                f"end_time_utc {self.end_time_utc.isoformat()} inconsistent with "
                f"npts={self.npts} at {self.sampling_rate_hz} Hz (expected "
                f"{expected_end.isoformat()} within {tolerance.total_seconds()} s)"
            )
        digest = hashlib.sha256(self.data).hexdigest()
        if digest != self.data_sha256:
            raise ValueError("data_sha256 does not match data")
        if self.encoding == TraceEncoding.float32le and len(self.data) != 4 * self.npts:
            raise ValueError(
                f"float32le payload must be 4*npts={4 * self.npts} bytes, got {len(self.data)}"
            )
        return self


def sha256_of_bytes(data: bytes) -> str:
    """Hex SHA-256 of a byte string (the `data_sha256` convention)."""
    return hashlib.sha256(data).hexdigest()


CONTRACTS: dict[str, type[BaseModel]] = {"seismic-trace": SeismicTrace}
