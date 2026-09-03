"""ICIMOD-reported hydrometric fixture adapter (offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from serac.adapters.hydro.icimod_fixture import (
    DEFAULT_FIXTURE,
    HydroFixtureError,
    IcimodReportedHydrometric,
    load_fixture,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.errors import DatasetNotFetchedError
from serac.ports.seismic import HydroObservation

DAY = (datetime(2026, 8, 26, tzinfo=UTC), datetime(2026, 8, 26, 23, 59, 59, tzinfo=UTC))


@pytest.fixture
def source(repo_root: Path) -> IcimodReportedHydrometric:
    return IcimodReportedHydrometric(repo_root / DEFAULT_FIXTURE)


def test_committed_fixture_is_fetched_and_fully_cited(source: IcimodReportedHydrometric) -> None:
    fixture = source.fixture
    assert fixture.status == "fetched"
    assert fixture.event_id == "langtang-lhende-2026"
    src = fixture.sources[0]
    assert src.url.startswith("https://www.icimod.org/press-release/")
    assert src.stored_copy is None  # all-rights-reserved page is cited, not stored
    assert src.licence_source_url == "https://www.icimod.org/terms-of-use/"
    stations = {s.station_id for s in source.stations()}
    assert stations == {"galchhi", "malekhu"}
    for station in source.stations():
        assert station.latitude is None and station.longitude is None  # not stated: null


def test_every_observation_quotes_its_sentence(source: IcimodReportedHydrometric) -> None:
    galchhi = source.observations("galchhi", DAY)
    malekhu = source.observations("malekhu", DAY)
    assert [o.value for o in galchhi] == [9.0]
    assert [o.value for o in malekhu] == [7.0]
    for obs in galchhi + malekhu:
        assert obs.variable == "stage_change_m"
        assert obs.interval_s == 1800
        assert obs.time_utc is None
        assert obs.time_basis.startswith("not_stated_in_source")
        assert obs.excerpt and "nine metres within 30 minutes" in obs.excerpt
        assert obs.source_ref == "icimod-media-advisory-2026-08-26"


def test_untimed_observations_match_only_the_event_day(source: IcimodReportedHydrometric) -> None:
    other = (datetime(2026, 8, 27, tzinfo=UTC), datetime(2026, 8, 28, tzinfo=UTC))
    with pytest.raises(DatasetNotFetchedError, match="no reported observations"):
        source.observations("galchhi", other)
    with pytest.raises(DatasetNotFetchedError, match="no station"):
        source.observations("betrawati", DAY)


def test_not_fetched_fixture_raises(tmp_path: Path) -> None:
    path = tmp_path / "hydro.json"
    path.write_text(json.dumps({"status": "not_fetched", "reason": "page unreachable"}))
    src = IcimodReportedHydrometric(path)
    with pytest.raises(DatasetNotFetchedError, match="not_fetched"):
        src.stations()
    with pytest.raises(DatasetNotFetchedError):
        src.observations("galchhi", DAY)
    with pytest.raises(DatasetNotFetchedError, match="missing"):
        IcimodReportedHydrometric(tmp_path / "absent.json").stations()


def test_fixture_rules(tmp_path: Path, repo_root: Path) -> None:
    doc = json.loads((repo_root / DEFAULT_FIXTURE).read_text())
    bad = dict(doc)
    bad["observations"] = [dict(doc["observations"][0], source_ref="unknown")]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(HydroFixtureError, match="unknown source_ref"):
        load_fixture(path)
    nf = {"status": "not_fetched", "stations": doc["stations"], "reason": "x"}
    path.write_text(json.dumps(nf))
    with pytest.raises(HydroFixtureError, match="must not carry data"):
        load_fixture(path)


def test_stage_change_requires_interval() -> None:
    with pytest.raises(ValidationError, match="interval_s"):
        HydroObservation(
            station_id="x", time_basis="gauge", variable="stage_change_m", value=1.0, source_ref="s"
        )


def test_fixture_has_a_ledger_row(repo_root: Path) -> None:
    ledger = JsonlManifestLedger(repo_root / "data" / "manifest.jsonl")
    rows = [e for e in ledger.entries() if e.source == DataSource.hydrometric_icimod]
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ManifestStatus.fetched
    assert row.path == DEFAULT_FIXTURE.as_posix()
    assert row.params["page_sha256"] == load_fixture(repo_root / DEFAULT_FIXTURE).sources[0].sha256
