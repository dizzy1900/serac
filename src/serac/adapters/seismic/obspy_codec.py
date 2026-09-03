"""ObsPy boundary: the only module allowed to import obspy in Prompt 1.

Converts between `obspy.Trace` and the bus contract `SeismicTrace`:

* integer samples are written as MiniSEED, Steim2, 512-byte records (the SeedLink native
  form, so a live record and a replay chunk look identical downstream);
* floating-point samples cannot be Steim2-encoded, so they are written as MiniSEED `FLOAT32`
  records (the fallback), never silently rounded to integers.

`slice_stream` turns an archived `Stream` into fixed-length chunks ordered by stream time
across all channels, which is the order replay publishes them in.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np
from obspy import Stream, Trace, UTCDateTime, read

from serac.domain.seismic import (
    SeismicTrace,
    Sncl,
    TraceEncoding,
    TraceProvenance,
    sha256_of_bytes,
)
from serac.errors import SeracError

MSEED_RECLEN = 512
MSEED_INT_ENCODING = "STEIM2"
MSEED_FLOAT_ENCODING = "FLOAT32"


class CodecBoundaryError(SeracError):
    """An ObsPy object could not be converted to or from a `SeismicTrace`."""


def _to_utc(value: UTCDateTime) -> datetime:
    return datetime.fromtimestamp(float(value.timestamp), tz=UTC)


def _to_obspy_time(value: datetime) -> UTCDateTime:
    return UTCDateTime(value.astimezone(UTC).timestamp())


def sncl_of(trace: Trace) -> Sncl:
    """SNCL of an ObsPy trace."""
    stats = trace.stats
    return Sncl(
        network=str(stats.network),
        station=str(stats.station),
        location=str(stats.location),
        channel=str(stats.channel),
    )


def trace_id_for(sncl: Sncl, start: datetime, sequence: int) -> str:
    """Deterministic chunk id: `NET.STA.LOC.CHA/<iso start>/<sequence>`."""
    return f"{sncl.key}/{start.astimezone(UTC).isoformat()}/{sequence}"


def encode_mseed(trace: Trace) -> tuple[bytes, TraceEncoding]:
    """MiniSEED bytes for a trace: Steim2 for integer data, FLOAT32 otherwise."""
    data = trace.data
    if data.size == 0:
        raise CodecBoundaryError("cannot encode an empty trace")
    copy = trace.copy()
    if np.issubdtype(data.dtype, np.integer):
        copy.data = np.require(data, dtype=np.int32)
        encoding = MSEED_INT_ENCODING
    else:
        copy.data = np.require(data, dtype=np.float32)
        encoding = MSEED_FLOAT_ENCODING
    # obspy refuses to write Steim2 when a stats.mseed encoding from a read file disagrees.
    if hasattr(copy.stats, "mseed"):
        del copy.stats.mseed
    buffer = io.BytesIO()
    copy.write(buffer, format="MSEED", encoding=encoding, reclen=MSEED_RECLEN)
    return buffer.getvalue(), TraceEncoding.miniseed


def trace_to_chunk(
    trace: Trace,
    *,
    provenance: TraceProvenance,
    sequence: int,
    quality: str | None = None,
) -> SeismicTrace:
    """Wrap an ObsPy trace as a bus chunk (MiniSEED payload)."""
    payload, encoding = encode_mseed(trace)
    sncl = sncl_of(trace)
    start = _to_utc(trace.stats.starttime)
    end = _to_utc(trace.stats.endtime)
    if quality is None:
        mseed_stats = getattr(trace.stats, "mseed", None)
        raw_quality = mseed_stats.get("dataquality") if mseed_stats is not None else None
        quality = str(raw_quality) if raw_quality in ("D", "R", "Q", "M") else None
    return SeismicTrace(
        trace_id=trace_id_for(sncl, start, sequence),
        sncl=sncl,
        start_time_utc=start,
        end_time_utc=end,
        sampling_rate_hz=float(trace.stats.sampling_rate),
        npts=int(trace.stats.npts),
        encoding=encoding,
        data=payload,
        data_sha256=sha256_of_bytes(payload),
        quality=quality,
        sequence=sequence,
        provenance=provenance,
    )


def chunk_to_trace(chunk: SeismicTrace) -> Trace:
    """Decode a bus chunk back into an ObsPy trace."""
    if chunk.encoding == TraceEncoding.miniseed:
        stream = read(io.BytesIO(chunk.data), format="MSEED")
        if len(stream) != 1:
            # A chunk is one contiguous segment by construction; gaps mean a bad producer.
            stream.merge(method=0, fill_value=None)
            if len(stream) != 1:
                raise CodecBoundaryError(
                    f"chunk {chunk.trace_id} decodes to {len(stream)} segments, expected 1"
                )
        trace: Trace = stream[0]
    else:
        samples = np.frombuffer(chunk.data, dtype="<f4")
        trace = Trace(data=np.array(samples, dtype=np.float32))
        trace.stats.network = chunk.sncl.network
        trace.stats.station = chunk.sncl.station
        trace.stats.location = chunk.sncl.location
        trace.stats.channel = chunk.sncl.channel
        trace.stats.sampling_rate = chunk.sampling_rate_hz
        trace.stats.starttime = _to_obspy_time(chunk.start_time_utc)
    if int(trace.stats.npts) != chunk.npts:
        raise CodecBoundaryError(
            f"chunk {chunk.trace_id}: payload has {trace.stats.npts} samples, header says "
            f"{chunk.npts}"
        )
    if sncl_of(trace) != chunk.sncl:
        raise CodecBoundaryError(
            f"chunk {chunk.trace_id}: payload SNCL {sncl_of(trace).key} != {chunk.sncl.key}"
        )
    return trace


def slice_stream(
    stream: Stream,
    *,
    chunk_seconds: float,
    provenance: TraceProvenance,
) -> Iterator[SeismicTrace]:
    """Cut every trace into `chunk_seconds` pieces and yield them in stream-time order.

    Chunks from different channels interleave by start time; ties break by SNCL key so the
    order is deterministic. Each channel's `sequence` counts its own chunks from zero. The
    last chunk of a trace may be shorter than `chunk_seconds`.
    """
    if chunk_seconds <= 0:
        raise CodecBoundaryError("chunk_seconds must be positive")
    pieces: list[tuple[datetime, str, int, Trace]] = []
    for trace in stream:
        if trace.stats.npts == 0:
            continue
        key = sncl_of(trace).key
        delta = float(trace.stats.delta)
        window_start = trace.stats.starttime
        sequence = 0
        while window_start <= trace.stats.endtime:
            window_end = window_start + chunk_seconds - delta
            piece = trace.slice(window_start, window_end, nearest_sample=False)
            if piece.stats.npts == 0:
                break
            pieces.append((_to_utc(piece.stats.starttime), key, sequence, piece))
            sequence += 1
            window_start = piece.stats.endtime + delta
    pieces.sort(key=lambda item: (item[0], item[1], item[2]))
    for _start, _key, sequence, piece in pieces:
        yield trace_to_chunk(piece, provenance=provenance, sequence=sequence)
