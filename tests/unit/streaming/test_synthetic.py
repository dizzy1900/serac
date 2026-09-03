from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest

from serac.domain.seismic import Sncl, TraceEncoding, TraceSource
from serac.streaming.synthetic import (
    DEFAULT_SNCL,
    synthetic_chunks,
    synthetic_lp_burst,
    synthetic_samples,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_chunks_are_labelled_synthetic_with_notes() -> None:
    chunks = list(synthetic_chunks(start_utc=T0, n_chunks=3))
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.provenance.source == TraceSource.synthetic
        assert chunk.provenance.notes is not None
        assert "not an observation" in chunk.provenance.notes
        assert chunk.is_synthetic
        assert chunk.encoding == TraceEncoding.float32le
        assert chunk.sncl == DEFAULT_SNCL
        assert len(chunk.data) == 4 * chunk.npts


def test_chunks_are_contiguous_and_sequenced() -> None:
    chunks = list(synthetic_chunks(start_utc=T0, n_chunks=4, chunk_seconds=5, sampling_rate_hz=20))
    assert [c.sequence for c in chunks] == [0, 1, 2, 3]
    assert all(c.npts == 100 for c in chunks)
    for earlier, later in pairwise(chunks):
        assert later.start_time_utc == earlier.end_time_utc + timedelta(seconds=0.05)
    assert chunks[0].start_time_utc == T0


def test_same_seed_same_bytes() -> None:
    a = [c.data for c in synthetic_chunks(start_utc=T0, n_chunks=2, seed=3)]
    b = [c.data for c in synthetic_chunks(start_utc=T0, n_chunks=2, seed=3)]
    c = [c.data for c in synthetic_chunks(start_utc=T0, n_chunks=2, seed=4)]
    assert a == b
    assert a != c


def test_custom_sncl_and_notes() -> None:
    sncl = Sncl.from_key("XX.TEST.01.HHZ")
    chunk = next(synthetic_chunks(start_utc=T0, n_chunks=1, sncl=sncl, notes="my note"))
    assert chunk.sncl == sncl
    assert chunk.provenance.notes == "my note"
    assert chunk.trace_id.startswith("XX.TEST.01.HHZ/")


def test_lp_burst_adds_energy_only_inside_the_burst() -> None:
    rate = 20.0
    n = int(120 * rate)
    noise = synthetic_samples(n=n, sampling_rate_hz=rate, kind="white_noise", seed=7)
    burst = synthetic_samples(
        n=n,
        sampling_rate_hz=rate,
        kind="lp_burst",
        seed=7,
        burst_start_s=40,
        burst_duration_s=40,
        burst_period_s=20,
        burst_amplitude=50,
    )
    t = np.arange(n) / rate
    inside = (t >= 40) & (t < 80)
    assert np.array_equal(noise[~inside], burst[~inside])
    assert burst[inside].std() > 10 * noise[inside].std()
    spectrum = np.abs(np.fft.rfft(burst[inside] - noise[inside]))
    freqs = np.fft.rfftfreq(int(inside.sum()), d=1 / rate)
    assert freqs[int(np.argmax(spectrum[1:])) + 1] == pytest.approx(0.05, abs=0.01)


def test_lp_burst_generator_defaults() -> None:
    chunks = list(synthetic_lp_burst(start_utc=T0))
    assert len(chunks) == 24
    assert chunks[0].provenance.notes is not None
    assert "lp_burst" in chunks[0].provenance.notes
    total = sum(c.npts for c in chunks)
    assert total == 24 * 100


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_chunks": 0},
        {"n_chunks": 1, "chunk_seconds": 0},
        {"n_chunks": 1, "sampling_rate_hz": 0},
        {"n_chunks": 1, "chunk_seconds": 0.001, "sampling_rate_hz": 1},
    ],
)
def test_rejects_bad_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        list(synthetic_chunks(start_utc=T0, **kwargs))  # type: ignore[arg-type]


def test_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="aware"):
        list(synthetic_chunks(start_utc=datetime(2026, 1, 1), n_chunks=1))
