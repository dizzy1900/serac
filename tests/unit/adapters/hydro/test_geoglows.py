"""GEOGLOWS ECMWF hydrometric adapter against a committed truncated real payload."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from serac.adapters.hydro.geoglows import (
    ADAPTER_NAME,
    DEFAULT_FIXTURE,
    DEFAULT_REACH_ID,
    LICENCE_SOURCE_URL,
    SOURCE_REF,
    GeoglowsError,
    GeoglowsHydrometric,
    getriverid_url,
    load_fixture_document,
    parse_flow_series,
    retrospectivedaily_url,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.errors import DatasetNotFetchedError

DAY = (datetime(2026, 8, 26, tzinfo=UTC), datetime(2026, 8, 26, 23, 59, 59, tzinfo=UTC))
WINDOW_AUG = (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 7, 23, 59, 59, tzinfo=UTC))
PROVENANCE = Path("data") / "fixtures" / "hydro" / "geoglows" / "provenance.json"


class FakeGeoglows:
    def __init__(self, by_url: dict[str, Any]) -> None:
        self.by_url = by_url
        self.urls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.urls.append(url)
        if url not in self.by_url:
            raise RuntimeError(f"unexpected url {url}")
        return self.by_url[url]


@pytest.fixture
def source(repo_root: Path) -> GeoglowsHydrometric:
    return GeoglowsHydrometric(repo_root / DEFAULT_FIXTURE)


def test_committed_fixture_is_a_real_truncated_payload(
    source: GeoglowsHydrometric, repo_root: Path
) -> None:
    payload = load_fixture_document(repo_root / DEFAULT_FIXTURE)
    assert "status" not in payload
    assert parse_flow_series(payload)
    stations = source.stations()
    assert [s.station_id for s in stations] == [DEFAULT_REACH_ID]
    assert stations[0].river == "Trishuli"
    prov = json.loads((repo_root / PROVENANCE).read_text(encoding="utf-8"))
    assert prov["fixture_truncated"] is True
    assert prov["full_payload_sha256"] == (
        "b5365cda5ab11bdd17b25af4731ac541675cf949e4a17e6250a57cb8728633a0"
    )
    assert prov["full_payload_size_bytes"] == 1_053_631
    assert prov["licence_source_url"] == LICENCE_SOURCE_URL
    assert "CC-BY-4.0" in prov["licence"]
    assert "Copernicus" in prov["licence"]


def test_observations_are_modelled_discharges_not_synthesised(
    source: GeoglowsHydrometric,
) -> None:
    obs = source.observations(DEFAULT_REACH_ID, DAY)
    assert len(obs) == 1
    assert obs[0].variable == "discharge_m3s"
    assert obs[0].value == 25.03
    assert obs[0].time_utc == datetime(2026, 8, 26, tzinfo=UTC)
    assert obs[0].source_ref == SOURCE_REF
    assert obs[0].notes and "not a DHM gauge" in obs[0].notes
    series = source.observations(DEFAULT_REACH_ID, WINDOW_AUG)
    assert len(series) == 38
    assert all(o.variable == "discharge_m3s" for o in series)


def test_unknown_station_and_empty_window_raise(source: GeoglowsHydrometric) -> None:
    with pytest.raises(DatasetNotFetchedError, match="no GEOGLOWS reach"):
        source.observations("galchhi", DAY)
    other = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC))
    with pytest.raises(DatasetNotFetchedError, match="never a synthesised"):
        source.observations(DEFAULT_REACH_ID, other)


def test_not_fetched_fixture_raises(tmp_path: Path) -> None:
    path = tmp_path / "geoglows.json"
    path.write_text(json.dumps({"status": "not_fetched", "reason": "API unreachable"}))
    src = GeoglowsHydrometric(path)
    with pytest.raises(DatasetNotFetchedError, match="not_fetched"):
        src.stations()
    with pytest.raises(DatasetNotFetchedError):
        src.observations(DEFAULT_REACH_ID, DAY)
    with pytest.raises(DatasetNotFetchedError, match="missing"):
        GeoglowsHydrometric(tmp_path / "absent.json").stations()


def test_malformed_payload_is_refused_not_filled() -> None:
    with pytest.raises(GeoglowsError, match="no datetime"):
        parse_flow_series({"441020026": [1.0]})


def test_fetch_uses_injected_client_and_does_not_invent(
    repo_root: Path,
) -> None:
    payload = load_fixture_document(repo_root / DEFAULT_FIXTURE)
    url = retrospectivedaily_url(DEFAULT_REACH_ID)
    fake = FakeGeoglows({url: payload})
    src = GeoglowsHydrometric(repo_root / DEFAULT_FIXTURE, client=fake)
    fetched = src.fetch_retrospective_daily(DEFAULT_REACH_ID)
    assert fake.urls == [url]
    assert fetched["441020026"][0] == payload["441020026"][0]


def test_fetch_failure_is_not_fetched() -> None:
    class Boom:
        def get_json(self, url: str) -> Any:
            raise OSError("offline")

    src = GeoglowsHydrometric(client=Boom())  # type: ignore[arg-type]
    with pytest.raises(DatasetNotFetchedError, match="fetch failed"):
        src.fetch_retrospective_daily(DEFAULT_REACH_ID)


def test_nearest_reach_from_fake_client() -> None:
    url = getriverid_url(27.81, 85.00)
    fake = FakeGeoglows({url: {"river_id": 441020026}})
    src = GeoglowsHydrometric(client=fake)
    assert src.nearest_reach_id(27.81, 85.00) == DEFAULT_REACH_ID
    assert fake.urls == [url]


def test_fixture_has_ledger_rows(repo_root: Path) -> None:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    rows = [e for e in ledger.entries() if e.source == DataSource.geoglows_ecmwf]
    paths = {e.path for e in rows}
    assert DEFAULT_FIXTURE.as_posix() in paths
    assert PROVENANCE.as_posix() in paths
    for row in rows:
        assert row.status is ManifestStatus.fetched
        assert row.provenance is Provenance.real
        assert row.adapter == ADAPTER_NAME
        assert row.licence_source_url == LICENCE_SOURCE_URL
        assert row.params.get("truncated") is True or row.product_id.endswith("provenance")
