"""`SyngineGreensLibrary`: the cache, the index, the ledger rows and the dry run.

Nothing here touches the network. A fake HTTP client returns MiniSEED assembled in-process,
which is enough to prove the parts that go wrong quietly: a `plan()` that writes something, a
ledger row that calls modelled physics an observation, or a cached file served after its bytes
have changed underneath the index.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from serac.adapters.seismic.syngine import (
    ELEMENTARY,
    LICENCE_SOURCE_URL,
    SyngineError,
    SyngineGreensLibrary,
    distance_library,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.ports.greens import EarthModel, GreensRequest


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.text = "" if status_code == 200 else "fake error"


class FakeSyngine:
    """Returns a deterministic three-component trace and counts the calls."""

    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status_code = status_code

    def get(self, url: str, *, params: Any = None, timeout: Any = None) -> FakeResponse:
        from obspy import Stream, Trace

        self.calls.append(dict(params or {}))
        distance = float((params or {}).get("receiverlongitude", 1.0))
        force = str((params or {}).get("sourceforce", "1,0,0"))
        seed = abs(hash((round(distance, 2), force))) % 10_000
        rng = np.random.default_rng(seed)
        stream = Stream()
        for index, component in enumerate("ZNE"):
            data = rng.normal(size=64).astype("float32") * 1e-18
            trace = Trace(data=data)
            trace.stats.channel = f"LX{component}"
            trace.stats.sampling_rate = 1.0
            trace.stats.station = f"S{index}"
            stream += trace
        buffer = io.BytesIO()
        stream.write(buffer, format="MSEED")
        return FakeResponse(buffer.getvalue(), self.status_code)


@pytest.fixture
def library(tmp_path: Path) -> tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger]:
    client = FakeSyngine()
    return (
        SyngineGreensLibrary(tmp_path / "greens", client=client, repo_root=tmp_path),
        client,
        JsonlManifestLedger(tmp_path / "manifest.jsonl"),
    )


def _request(distance: float = 3.0) -> GreensRequest:
    return GreensRequest(
        model=EarthModel.prem_a_20s, distance_deg=distance, source_depth_m=1000.0, dt_s=1.0
    )


# --- plan ------------------------------------------------------------------------------------


def test_plan_writes_nothing_at_all(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger], tmp_path: Path
) -> None:
    """Not the cache, not the index, not a ledger line -- a dry run leaves no trace."""
    lib, client, _ledger = library
    requests = [_request(d) for d in (1.0, 2.0, 3.0)]
    plan = lib.plan(requests)

    assert plan.to_fetch == 3 and plan.cached == 0
    assert plan.estimated_bytes > 0 and plan.estimate_basis
    assert plan.provider_url.startswith("https://")
    assert not client.calls, "plan() must not call the service"
    assert not (tmp_path / "greens").exists()
    assert not (tmp_path / "manifest.jsonl").exists()


def test_plan_deduplicates_and_counts_what_is_already_cached(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    lib, _client, ledger = library
    lib.get(_request(2.0), ledger)
    plan = lib.plan([_request(2.0), _request(2.0), _request(4.0)])
    assert len(plan.requests) == 2, "duplicate cache keys collapse"
    assert (plan.cached, plan.to_fetch) == (1, 1)


def test_distance_library_covers_the_stated_range() -> None:
    requests = distance_library(min_deg=0.5, max_deg=15.0, step_deg=0.05)
    assert len(requests) == 291
    assert requests[0].distance_deg == pytest.approx(0.5)
    assert requests[-1].distance_deg == pytest.approx(15.0)
    assert len({r.cache_key() for r in requests}) == len(requests)


# --- fetch, cache and index --------------------------------------------------------------------


def test_two_requests_per_distance_produce_five_elementary_traces(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    """The whole reason a 0.5-15 degree library is 582 requests rather than 8712."""
    lib, client, ledger = library
    greens = lib.get(_request(), ledger)
    assert len(client.calls) == 2, "one vertical force, one superposed horizontal force"
    assert [(t.force_component, t.receiver_component) for t in greens.traces] == list(ELEMENTARY)
    assert greens.modelled is True
    assert len(greens.sha256) == 64


def test_a_second_get_is_served_from_disk(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    lib, client, ledger = library
    first = lib.get(_request(), ledger)
    calls_after_first = len(client.calls)
    second = lib.get(_request(), ledger)
    assert len(client.calls) == calls_after_first, "a cache hit makes no request"
    assert second.sha256 == first.sha256
    np.testing.assert_allclose(
        second.traces[0].samples_m_per_n, first.traces[0].samples_m_per_n, rtol=1e-6
    )


def test_the_index_records_path_sha256_and_time(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    lib, _client, ledger = library
    request = _request()
    greens = lib.get(request, ledger)
    index = json.loads(lib.index_path(request.model).read_text(encoding="utf-8"))
    entry = index["entries"][request.cache_key()]
    assert entry["sha256"] == greens.sha256
    assert entry["path"].endswith(f"{request.cache_key()}.json.gz")
    assert entry["retrieved_at"]
    assert index["modelled"] is True


def test_a_cache_file_that_no_longer_matches_its_index_is_refused(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    """Silently inverting on altered Green's functions is worse than failing."""
    lib, _client, ledger = library
    request = _request()
    lib.get(request, ledger)
    path = lib.cache_path(request)
    payload = json.loads(gzip.decompress(path.read_bytes()))
    payload["provider"] = "tampered"
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))
    lib._index_cache.clear()
    with pytest.raises(SyngineError, match="does not match its index"):
        lib.get(request, ledger)


def test_offline_library_refuses_rather_than_fetching(tmp_path: Path) -> None:
    lib = SyngineGreensLibrary(tmp_path / "greens", allow_network=False, repo_root=tmp_path)
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(SyngineError, match="allow_network=False"):
        lib.get(_request(), ledger)


def test_a_non_200_response_raises(tmp_path: Path) -> None:
    lib = SyngineGreensLibrary(
        tmp_path / "greens", client=FakeSyngine(status_code=503), repo_root=tmp_path
    )
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(SyngineError, match="HTTP 503"):
        lib.get(_request(), ledger)


# --- provenance ---------------------------------------------------------------------------------


def test_the_ledger_row_calls_it_derived_and_modelled(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    """ADR-0016: modelled physics is `derived`, never `synthetic` and never `real`."""
    lib, _client, ledger = library
    lib.get(_request(), ledger)
    rows = list(ledger.entries())
    assert len(rows) == 1
    row = rows[0]
    assert row.source is DataSource.iris_syngine
    assert row.provenance is Provenance.derived
    assert row.status is ManifestStatus.fetched
    assert row.params["modelled"] is True
    assert row.params["earth_model"] == "prem_a_20s"
    assert len(row.params["requests"]) == 2, "both Syngine URLs are recorded"
    assert row.licence_source_url == LICENCE_SOURCE_URL
    assert row.sha256 and row.size_bytes and row.retrieved_at
    assert row.path is not None and row.path.startswith("greens/")


def test_a_cache_hit_does_not_append_a_second_ledger_row(
    library: tuple[SyngineGreensLibrary, FakeSyngine, JsonlManifestLedger],
) -> None:
    lib, _client, ledger = library
    lib.get(_request(), ledger)
    lib.get(_request(), ledger)
    assert len(list(ledger.entries())) == 1


def test_greens_functions_are_never_expressible_as_a_seismic_trace() -> None:
    """A `GreensSet` carries `modelled: True` as a `Literal`, so the fact cannot be dropped."""
    from serac.ports.greens import GreensSet

    field = GreensSet.model_fields["modelled"]
    assert field.default is True
    with pytest.raises(ValueError):
        GreensSet.model_validate(
            {
                "request": json.loads(_request().model_dump_json()),
                "traces": [
                    {
                        "force_component": "up",
                        "receiver_component": "Z",
                        "samples_m_per_n": [1.0],
                    }
                ],
                "provider": "x",
                "provider_url": "x",
                "cache_key": "x",
                "sha256": "0" * 64,
                "modelled": False,
            }
        )
