from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime, read

from serac.adapters.seismic.obspy_codec import (
    MSEED_RECLEN,
    CodecBoundaryError,
    chunk_to_trace,
    encode_mseed,
    slice_stream,
    sncl_of,
    trace_to_chunk,
)
from serac.domain.seismic import TraceEncoding, TraceProvenance, TraceSource
from serac.streaming.synthetic import synthetic_chunks

PROV = TraceProvenance(source=TraceSource.synthetic, notes="codec unit test")
T0 = UTCDateTime(2021, 2, 7, 4, 51)


def int_trace(npts: int = 1000, rate: float = 50.0, sta: str = "KKN") -> Trace:
    data = (np.arange(npts, dtype=np.int64) * 37 % 1001 - 500).astype(np.int32)
    tr = Trace(data=data)
    tr.stats.network, tr.stats.station, tr.stats.channel = "NK", sta, "BHZ"
    tr.stats.sampling_rate = rate
    tr.stats.starttime = T0
    return tr


def float_trace(npts: int = 1000, rate: float = 20.0) -> Trace:
    tr = int_trace(npts, rate, sta="LSA")
    tr.data = np.random.default_rng(1).normal(size=npts).astype(np.float32)
    return tr


class TestTraceRoundTrip:
    def test_int_data_is_steim2_512(self) -> None:
        tr = int_trace()
        chunk = trace_to_chunk(tr, provenance=PROV, sequence=4)
        assert chunk.encoding == TraceEncoding.miniseed
        assert chunk.npts == 1000
        assert chunk.sequence == 4
        assert chunk.sncl.key == "NK.KKN..BHZ"
        assert chunk.start_time_utc == datetime(2021, 2, 7, 4, 51, tzinfo=UTC)
        assert len(chunk.data) % MSEED_RECLEN == 0
        st = read(io.BytesIO(chunk.data))
        assert st[0].stats.mseed.encoding == "STEIM2"
        assert st[0].stats.mseed.record_length == MSEED_RECLEN
        back = chunk_to_trace(chunk)
        assert np.array_equal(back.data, tr.data)
        assert back.stats.starttime == tr.stats.starttime
        assert back.stats.sampling_rate == tr.stats.sampling_rate

    def test_float_data_falls_back_to_float32(self) -> None:
        tr = float_trace()
        chunk = trace_to_chunk(tr, provenance=PROV, sequence=0)
        assert chunk.encoding == TraceEncoding.miniseed
        st = read(io.BytesIO(chunk.data))
        assert st[0].stats.mseed.encoding == "FLOAT32"
        back = chunk_to_trace(chunk)
        assert np.array_equal(back.data, tr.data)

    def test_float64_is_not_rounded_to_int(self) -> None:
        tr = float_trace()
        tr.data = tr.data.astype(np.float64) + 0.25
        _, encoding = encode_mseed(tr)
        back = chunk_to_trace(trace_to_chunk(tr, provenance=PROV, sequence=0))
        assert encoding == TraceEncoding.miniseed
        assert back.data.dtype == np.float32
        assert np.allclose(back.data, tr.data)

    def test_empty_trace_rejected(self) -> None:
        with pytest.raises(CodecBoundaryError, match="empty"):
            trace_to_chunk(int_trace(0), provenance=PROV, sequence=0)

    def test_quality_from_mseed_stats(self) -> None:
        tr = int_trace()
        first = trace_to_chunk(tr, provenance=PROV, sequence=0)
        assert first.quality is None
        reread = read(io.BytesIO(first.data))[0]
        assert trace_to_chunk(reread, provenance=PROV, sequence=1).quality == "D"
        assert trace_to_chunk(reread, provenance=PROV, sequence=1, quality="Q").quality == "Q"

    def test_sncl_mismatch_detected(self) -> None:
        chunk = trace_to_chunk(int_trace(), provenance=PROV, sequence=0)
        tampered = chunk.model_copy(update={"sncl": chunk.sncl.model_copy(update={"station": "X"})})
        with pytest.raises(CodecBoundaryError, match="SNCL"):
            chunk_to_trace(tampered)

    def test_npts_mismatch_detected(self) -> None:
        chunk = trace_to_chunk(int_trace(), provenance=PROV, sequence=0)
        tampered = chunk.model_copy(update={"npts": 999})
        with pytest.raises(CodecBoundaryError, match="samples"):
            chunk_to_trace(tampered)


class TestFloat32leChunks:
    def test_synthetic_chunks_decode(self) -> None:
        chunk = next(
            synthetic_chunks(
                start_utc=datetime(2026, 1, 1, tzinfo=UTC),
                n_chunks=1,
                chunk_seconds=5,
                sampling_rate_hz=20,
            )
        )
        tr = chunk_to_trace(chunk)
        assert tr.stats.npts == 100
        assert tr.stats.sampling_rate == 20
        assert sncl_of(tr) == chunk.sncl
        assert tr.data.dtype == np.float32
        assert np.array_equal(tr.data, np.frombuffer(chunk.data, dtype="<f4"))


class TestSliceStream:
    def test_slices_preserve_samples_and_order(self) -> None:
        a = int_trace(npts=1000, rate=50, sta="KKN")  # 20 s
        b = float_trace(npts=300, rate=20)  # 15 s
        chunks = list(slice_stream(Stream([a, b]), chunk_seconds=5, provenance=PROV))
        assert [(c.sncl.station, c.sequence, c.npts) for c in chunks] == [
            ("KKN", 0, 250),
            ("LSA", 0, 100),
            ("KKN", 1, 250),
            ("LSA", 1, 100),
            ("KKN", 2, 250),
            ("LSA", 2, 100),
            ("KKN", 3, 250),
        ]
        starts = [c.start_time_utc for c in chunks]
        assert starts == sorted(starts)
        kkn = np.concatenate([chunk_to_trace(c).data for c in chunks if c.sncl.station == "KKN"])
        assert np.array_equal(kkn, a.data)
        lsa = np.concatenate([chunk_to_trace(c).data for c in chunks if c.sncl.station == "LSA"])
        assert np.array_equal(lsa, b.data)

    def test_short_tail_chunk(self) -> None:
        chunks = list(slice_stream(Stream([int_trace(npts=260)]), chunk_seconds=5, provenance=PROV))
        assert [c.npts for c in chunks] == [250, 10]

    def test_bad_chunk_seconds(self) -> None:
        with pytest.raises(CodecBoundaryError):
            list(slice_stream(Stream([int_trace()]), chunk_seconds=0, provenance=PROV))

    def test_skips_empty_traces(self) -> None:
        assert list(slice_stream(Stream([int_trace(0)]), chunk_seconds=5, provenance=PROV)) == []


@pytest.mark.parametrize(
    "fixture",
    sorted(Path(__file__).resolve().parents[4].joinpath("data/fixtures/seismic").glob("*/*.mseed")),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_real_fixture_round_trips_through_chunks(fixture: Path, repo_root: Path) -> None:
    stream = read(str(fixture), format="MSEED")
    assert len(stream) == 1, "fixtures are expected to be one contiguous segment"
    original = stream[0]
    provenance = TraceProvenance(
        source=TraceSource.fixture, fixture_path=fixture.relative_to(repo_root).as_posix()
    )
    chunks = list(slice_stream(stream, chunk_seconds=5.0, provenance=provenance))
    assert chunks, "no chunks produced"
    assert all(c.encoding == TraceEncoding.miniseed for c in chunks)
    assert all(c.quality == original.stats.mseed.dataquality for c in chunks)
    assert [c.sequence for c in chunks] == list(range(len(chunks)))
    assert sum(c.npts for c in chunks) == original.stats.npts
    decoded = [chunk_to_trace(c) for c in chunks]
    assert all(np.issubdtype(t.data.dtype, np.integer) for t in decoded)
    rebuilt = np.concatenate([t.data for t in decoded])
    assert np.array_equal(rebuilt, original.data)
    assert decoded[0].stats.starttime == original.stats.starttime
    assert decoded[-1].stats.endtime == original.stats.endtime
