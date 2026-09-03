"""ComCat adapter on the committed 57-event fixture and MockTransport pages (offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from serac.adapters.seismic.usgs_comcat import (
    LICENCE,
    ComCatCatalog,
    ComCatError,
    filter_events,
    load_fixture,
    page_name,
    parse_geojson,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.ports.seismic import CatalogQuery

FIXTURE = "landslide_2000-01-01_2026-09-03.geojson"


@pytest.fixture
def landslides(fixtures_dir: Path) -> Path:
    return fixtures_dir / "usgs_comcat" / FIXTURE


def test_fixture_parses_and_contains_langtang_events(landslides: Path) -> None:
    events = load_fixture(landslides)
    assert len(events) == 57
    by_id = {e.event_id: e for e in events}
    tbwb = by_id["us7000tbwb"]
    assert tbwb.time_utc == datetime(2026, 8, 26, 2, 52, 10, tzinfo=UTC)
    assert (tbwb.latitude, tbwb.longitude) == (28.271, 85.515)
    assert tbwb.magnitude == 5.2 and tbwb.mag_type == "ms_vx"
    assert tbwb.event_type == "landslide"
    assert "us7000tc90" in by_id
    assert by_id["us7000tc90"].time_utc == datetime(2026, 8, 26, 6, 0, 35, tzinfo=UTC)


def test_type_case_is_normalised(landslides: Path) -> None:
    raw = json.loads(landslides.read_text())
    raw_types = {f["properties"]["type"] for f in raw["features"]}
    assert raw_types == {"landslide", "Landslide"}  # the fixture really is inconsistent
    assert {e.event_type for e in parse_geojson(raw)} == {"landslide"}


def test_chamoli_window_is_empty_in_the_landslide_set(landslides: Path) -> None:
    query = CatalogQuery(
        start_utc=datetime(2021, 2, 6, tzinfo=UTC), end_utc=datetime(2021, 2, 9, tzinfo=UTC)
    )
    assert filter_events(load_fixture(landslides), query) == []


def test_filter_by_bbox_and_magnitude(landslides: Path) -> None:
    query = CatalogQuery(
        start_utc=datetime(2026, 8, 1, tzinfo=UTC),
        end_utc=datetime(2026, 9, 1, tzinfo=UTC),
        bbox_4326=(84.0, 27.0, 87.0, 29.0),
        min_magnitude=4.0,
    )
    ids = sorted(e.event_id for e in filter_events(load_fixture(landslides), query))
    assert ids == ["us7000tbwb", "us7000tc90"]


def test_parse_rejects_non_geojson() -> None:
    with pytest.raises(ComCatError, match="FeatureCollection"):
        parse_geojson({"type": "Nope"})
    with pytest.raises(ComCatError):
        parse_geojson({"type": "FeatureCollection", "features": [{"id": "x"}]})


def _paged_transport(
    features: list[dict[str, Any]],
) -> tuple[httpx.MockTransport, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        limit = int(params["limit"])
        offset = int(params["offset"])
        page = features[offset - 1 : offset - 1 + limit]
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "metadata": {"count": len(page)}, "features": page},
        )

    return httpx.MockTransport(handler), seen


def test_offset_pagination_walks_every_page(landslides: Path, tmp_path: Path) -> None:
    features = json.loads(landslides.read_text())["features"]
    transport, seen = _paged_transport(features)
    catalog = ComCatCatalog(httpx.Client(transport=transport), page_size=25, repo_root=tmp_path)
    query = CatalogQuery(
        start_utc=datetime(2000, 1, 1, tzinfo=UTC), end_utc=datetime(2026, 9, 3, tzinfo=UTC)
    )
    events = catalog.query(query)
    assert len(events) == 57
    assert [p["offset"] for p in seen] == ["1", "26", "51"]
    assert all(p["limit"] == "25" and p["eventtype"] == "landslide" for p in seen)
    assert all(p["format"] == "geojson" for p in seen)
    assert events[-1].event_id == features[-1]["id"]


def test_query_limit_caps_the_walk(landslides: Path) -> None:
    features = json.loads(landslides.read_text())["features"]
    transport, seen = _paged_transport(features)
    catalog = ComCatCatalog(httpx.Client(transport=transport), page_size=10)
    query = CatalogQuery(
        start_utc=datetime(2000, 1, 1, tzinfo=UTC),
        end_utc=datetime(2026, 9, 3, tzinfo=UTC),
        limit=15,
    )
    assert len(catalog.query(query)) == 15
    assert [p["limit"] for p in seen] == ["10", "5"]


def test_fetch_writes_pages_and_ledger_rows(landslides: Path, tmp_path: Path) -> None:
    features = json.loads(landslides.read_text())["features"]
    transport, _ = _paged_transport(features)
    catalog = ComCatCatalog(httpx.Client(transport=transport), page_size=30, repo_root=tmp_path)
    query = CatalogQuery(
        start_utc=datetime(2000, 1, 1, tzinfo=UTC), end_utc=datetime(2026, 9, 3, tzinfo=UTC)
    )
    ledger = JsonlManifestLedger(tmp_path / "data" / "manifest.jsonl")
    written = catalog.fetch(query, tmp_path / "data" / "raw", ledger)
    assert [p.name for p in written] == [
        "landslide_2000-01-01_2026-09-03.geojson",
        "landslide_2000-01-01_2026-09-03.p2.geojson",
    ]
    entries = list(ledger.entries())
    assert len(entries) == 2
    for entry, path in zip(entries, written, strict=True):
        assert entry.source == DataSource.usgs_comcat
        assert entry.status == ManifestStatus.fetched
        assert entry.licence == LICENCE
        assert entry.path == path.relative_to(tmp_path).as_posix()
        assert entry.size_bytes == path.stat().st_size
        assert entry.url is not None and "eventtype=landslide" in entry.url
    doc = json.loads(written[0].read_text())
    assert len(doc["features"]) == 30


def test_http_error_is_wrapped() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(503, text="down"))
    catalog = ComCatCatalog(httpx.Client(transport=transport))
    query = CatalogQuery(
        start_utc=datetime(2000, 1, 1, tzinfo=UTC), end_utc=datetime(2001, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(ComCatError, match="503"):
        catalog.query(query)


def test_plan_is_a_dry_run_with_stated_basis() -> None:
    catalog = ComCatCatalog(
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    )
    query = CatalogQuery(
        start_utc=datetime(2000, 1, 1, tzinfo=UTC), end_utc=datetime(2026, 9, 3, tzinfo=UTC)
    )
    plan = catalog.plan(query).as_dict()
    assert plan["estimated_bytes"] is None
    assert "41870 B / 57 events" in plan["estimate_basis"]
    assert "eventtype=landslide" in plan["url"]
    assert page_name(query, page=1) == "landslide_2000-01-01_2026-09-03"
    assert page_name(query, page=3) == "landslide_2000-01-01_2026-09-03.p3"
