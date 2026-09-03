"""FDSN archive adapter against a fake client built from the committed fixtures (offline)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from obspy import Inventory, read_inventory

from serac.adapters.seismic.fdsn import (
    ASSUMED_BYTES_PER_SAMPLE,
    FdsnAdapterError,
    FdsnWaveformArchive,
    bulk_rows,
    haversine_km,
    stations_from_inventory,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestStatus, Provenance
from serac.domain.replay import FixtureManifest
from serac.domain.seismic import Sncl
from serac.ports.seismic import StationQuery, WaveformRequest

T0 = datetime(2021, 2, 7, 4, 49, tzinfo=UTC)
T1 = datetime(2021, 2, 7, 4, 57, tzinfo=UTC)
KKN = Sncl(network="NK", station="KKN", location="", channel="BHZ")
LSA = Sncl(network="IC", station="LSA", location="00", channel="BHZ")
NOPE = Sncl(network="XX", station="NOPE", location="", channel="BHZ")


class FakeFdsnClient:
    """Serves the committed chamoli-2021 fixture bytes; records every call."""

    def __init__(self, fixture_dir: Path, *, base_url: str = "https://service.earthscope.org"):
        self.base_url = base_url
        self.fixture_dir = fixture_dir
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_stations(self, **kwargs: Any) -> Inventory:
        self.calls.append(("get_stations", kwargs))
        xml = self.fixture_dir / "stations.xml"
        filename = kwargs.get("filename")
        if isinstance(filename, io.BytesIO):
            filename.write(xml.read_bytes())
        return read_inventory(str(xml))

    def get_waveforms_bulk(self, bulk: Any, **kwargs: Any) -> None:
        self.calls.append(("get_waveforms_bulk", {"bulk": bulk, **kwargs}))
        (net, sta, loc, cha, _start, _end) = bulk[0]
        key = f"{net}.{sta}.{'' if loc == '--' else loc}.{cha}"
        path = self.fixture_dir / f"{key}.mseed"
        if not path.exists():
            raise Exception("No data available for request. HTTP Status code: 204")
        filename = kwargs["filename"]
        filename.write(path.read_bytes())


@pytest.fixture
def fixture_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "seismic" / "chamoli-2021"


@pytest.fixture
def archive(fixture_dir: Path, tmp_path: Path) -> FdsnWaveformArchive:
    return FdsnWaveformArchive(FakeFdsnClient(fixture_dir), repo_root=tmp_path)


def test_haversine_known_distance() -> None:
    # Kakani (NK.KKN) to Lhasa (IC.LSA): roughly 610 km on a sphere.
    d = haversine_km(27.8, 85.279, 29.70317, 91.12757)
    assert 600 < d < 620
    assert haversine_km(0, 0, 0, 0) == 0


def test_stations_from_committed_stationxml(fixture_dir: Path) -> None:
    inventory = read_inventory(str(fixture_dir / "stations.xml"))
    refs = stations_from_inventory(
        inventory, data_centre="https://service.earthscope.org", origin=(28.271, 85.515)
    )
    keys = [r.sncl.key for r in refs]
    assert keys == ["NK.KKN..BHZ", "IC.LSA.00.BHZ"]  # sorted by distance from the origin
    kkn = refs[0]
    assert kkn.sampling_rate_hz == 50.0
    assert kkn.distance_km is not None and 40 < kkn.distance_km < 70
    assert kkn.data_centre == "https://service.earthscope.org"
    assert refs[1].sampling_rate_hz == 20.0


def test_bulk_rows_write_empty_location_as_dashes() -> None:
    rows = bulk_rows([KKN, LSA], T0, T1)
    assert rows[0] == ["NK", "KKN", "--", "BHZ", "2021-02-07T04:49:00", "2021-02-07T04:57:00"]
    assert rows[1][2] == "00"


def test_base_url_is_resolved_not_alias(archive: FdsnWaveformArchive) -> None:
    assert archive.base_url == "https://service.earthscope.org"
    assert archive.terms()[0] == "https://www.earthscope.org/terms-of-service/"


def test_alias_as_base_url_is_refused(fixture_dir: Path) -> None:
    archive = FdsnWaveformArchive(FakeFdsnClient(fixture_dir, base_url="IRIS"))
    with pytest.raises(FdsnAdapterError, match="resolved"):
        _ = archive.base_url


def test_search_stations_by_radius(archive: FdsnWaveformArchive) -> None:
    refs = archive.find_stations(
        StationQuery(
            latitude=28.271, longitude=85.515, max_radius_km=1000, start_utc=T0, end_utc=T1
        )
    )
    assert [r.sncl.key for r in refs] == ["NK.KKN..BHZ", "IC.LSA.00.BHZ"]
    client = archive.client
    assert isinstance(client, FakeFdsnClient)
    kwargs = client.calls[0][1]
    assert kwargs["level"] == "channel"
    assert 8.9 < kwargs["maxradius"] < 9.1  # 1000 km in degrees


def test_plan_offline_with_explicit_stations_states_assumptions(fixture_dir: Path) -> None:
    archive = FdsnWaveformArchive(FakeFdsnClient(fixture_dir), lookup_metadata_for_plan=False)
    request = WaveformRequest(event_id="chamoli-2021", sncls=[KKN, LSA], start_utc=T0, end_utc=T1)
    plan = archive.plan(request)
    assert plan.data_centre == "https://service.earthscope.org"
    assert plan.bulk == bulk_rows([KKN, LSA], T0, T1)
    duration = (T1 - T0).total_seconds()
    assert plan.estimated_bytes == int(2 * duration * 50 * ASSUMED_BYTES_PER_SAMPLE + 5000)
    assert "bytes/sample" in plan.estimate_basis
    assert len(plan.warnings) == 2 and all("assumed 50.0 Hz" in w for w in plan.warnings)
    assert plan.refusals == []


def test_plan_uses_channel_metadata_when_available(archive: FdsnWaveformArchive) -> None:
    request = WaveformRequest(event_id="chamoli-2021", sncls=[KKN, LSA], start_utc=T0, end_utc=T1)
    plan = archive.plan(request)
    duration = (T1 - T0).total_seconds()
    assert plan.estimated_bytes == int(duration * (50 + 20) * ASSUMED_BYTES_PER_SAMPLE + 5000)
    assert plan.warnings == []


def test_fetch_writes_files_manifest_and_ledger(
    archive: FdsnWaveformArchive, tmp_path: Path, fixture_dir: Path
) -> None:
    request = WaveformRequest(event_id="chamoli-2021", sncls=[KKN, LSA], start_utc=T0, end_utc=T1)
    plan = archive.plan(request)
    dest = tmp_path / "data" / "raw" / "fdsn_waveforms" / "chamoli-2021"
    ledger = JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")
    result = archive.fetch(plan, dest, ledger)

    assert result.status == "fetched"
    assert result.missing == []
    assert (dest / "NK.KKN..BHZ.mseed").read_bytes() == (
        fixture_dir / "NK.KKN..BHZ.mseed"
    ).read_bytes()
    assert (dest / "stations.xml").exists()
    manifest = FixtureManifest.model_validate_json((dest / "manifest.json").read_text())
    assert manifest.status == "fetched"
    assert manifest.request.base_url == "https://service.earthscope.org"
    assert manifest.request.client == "EARTHSCOPE"
    assert manifest.licence is None
    assert manifest.licence_source_url == "https://www.earthscope.org/terms-of-service/"
    kinds = sorted(f.kind for f in manifest.files)
    assert kinds == ["miniseed", "miniseed", "stationxml"]
    kkn = next(f for f in manifest.files if f.sncl == "NK.KKN..BHZ")
    assert kkn.sampling_rate_hz == 50.0 and kkn.npts == 24001
    assert kkn.sha256 == sha256_of_file(fixture_dir / "NK.KKN..BHZ.mseed")
    assert kkn.url is not None and "fdsnws/dataselect/1/query?net=NK&sta=KKN&loc=--" in kkn.url

    entries = list(ledger.entries())
    assert len(entries) == 3 == len(result.entries)
    for entry in entries:
        assert entry.source == DataSource.fdsn_waveforms
        assert entry.status == ManifestStatus.fetched
        assert entry.provenance == Provenance.real
        assert entry.path is not None and entry.path.startswith("data/raw/fdsn_waveforms/")
        assert entry.licence == "null: see licence_source_url"
        assert entry.licence_source_url == "https://www.earthscope.org/terms-of-service/"
        assert entry.params["base_url"] == "https://service.earthscope.org"
        assert entry.sha256 == sha256_of_file(tmp_path / entry.path)
    assert "data/raw/fdsn_waveforms/chamoli-2021/manifest.json" in result.files


def test_fetch_partial_when_a_channel_has_no_data(
    archive: FdsnWaveformArchive, tmp_path: Path
) -> None:
    request = WaveformRequest(event_id="chamoli-2021", sncls=[KKN, NOPE], start_utc=T0, end_utc=T1)
    plan = FdsnWaveformArchive(archive.client, lookup_metadata_for_plan=False).plan(request)
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    result = archive.fetch(plan, tmp_path / "out", ledger)
    assert result.status == "partial"
    assert result.missing == ["XX.NOPE..BHZ"]
    manifest = FixtureManifest.model_validate_json((tmp_path / "out" / "manifest.json").read_text())
    assert manifest.status == "partial" and manifest.missing == ["XX.NOPE..BHZ"]


def test_fetch_nothing_records_not_fetched(archive: FdsnWaveformArchive, tmp_path: Path) -> None:
    request = WaveformRequest(event_id="nowhere", sncls=[NOPE], start_utc=T0, end_utc=T1)
    plan = FdsnWaveformArchive(archive.client, lookup_metadata_for_plan=False).plan(request)
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    result = archive.fetch(plan, tmp_path / "out", ledger)
    assert result.status == "not_fetched"
    entries = list(ledger.entries())
    assert len(entries) == 1 and entries[0].status == ManifestStatus.not_fetched
    assert not (tmp_path / "out" / "stations.xml").exists()


def test_fetch_refuses_a_plan_with_refusals(archive: FdsnWaveformArchive, tmp_path: Path) -> None:
    request = WaveformRequest(event_id="x", sncls=[KKN], start_utc=T0, end_utc=T1)
    plan = archive.plan(request).model_copy(update={"refusals": ["too big"]})
    with pytest.raises(FdsnAdapterError, match="refusals"):
        archive.fetch(plan, tmp_path, JsonlManifestLedger(tmp_path / "m.jsonl"))


def test_lazy_client_is_not_built_offline(fixture_dir: Path) -> None:
    archive = FdsnWaveformArchive(client_name="EARTHSCOPE")
    assert archive._client is None  # nothing touched the network at construction
