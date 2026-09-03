"""The AOI validation suite on a fictional AOI (passes) and deliberately broken copies."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from serac.domain.common import Range, SourceKind, SourceRef
from serac.domain.events import AssetType
from serac.domain.geo import AssetStatus
from serac.pipelines.aoi_build import (
    AoiSpec,
    AssetSpec,
    StatedLocation,
    TransectSpec,
    build_aoi,
    write_aoi_dir,
)
from serac.validation.aoi import run_suite
from serac.validation.result import Severity

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHA_A = "a" * 64

# An obviously fictional river: a straight north-south "river" in the Gulf of Guinea (0,0),
# three "streams" joining it, and two "places". Nothing here describes a real feature.
FICTIONAL_OVERPASS = {
    "version": 0.6,
    "generator": "fictional",
    "osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z", "copyright": "fictional test data"},
    "elements": [
        {
            "type": "way",
            "id": 1,
            "tags": {"waterway": "river", "name": "Test River"},
            "geometry": [{"lon": 0.0, "lat": 0.20 - 0.01 * i} for i in range(0, 41)],
        },
        {
            "type": "way",
            "id": 2,
            "tags": {"waterway": "stream", "name": "Test Tributary"},
            "geometry": [
                {"lon": 0.05, "lat": 0.15},
                {"lon": 0.02, "lat": 0.12},
                {"lon": 0.0, "lat": 0.10},
            ],
        },
        {
            "type": "way",
            "id": 3,
            "tags": {"waterway": "stream"},
            "geometry": [{"lon": 0.10, "lat": 0.25}, {"lon": 0.08, "lat": 0.22}],
        },
        {
            "type": "node",
            "id": 10,
            "lon": 0.001,
            "lat": 0.10,
            "tags": {"place": "village", "name": "Test Mid"},
        },
        {
            "type": "node",
            "id": 11,
            "lon": -0.002,
            "lat": -0.15,
            "tags": {"place": "town", "name": "Test End"},
        },
    ],
}


class DictOverpassClient:
    def __init__(self, doc: dict[str, object]) -> None:
        self.raw = json.dumps(doc).encode()

    def query(self, ql: str) -> bytes:
        return self.raw


FICTIONAL_SOURCE = SourceRef(
    id="test-src-agency",
    kind=SourceKind.agency_official,
    title="Fictional agency page",
    url="https://example.invalid/agency",
    accessed_utc=NOW,
    sha256=SHA_A,
    content_type="text/html",
    licence="CC-BY-4.0",
    claims_supported=["exposed_assets.test-plant.capacity_mw"],
    peer_reviewed=False,
)

FICTIONAL_PRESS = SourceRef(
    id="test-src-press",
    kind=SourceKind.press_report,
    title="Fictional press report",
    url="https://example.invalid/press",
    accessed_utc=NOW,
    sha256=SHA_A,
    content_type="text/html",
    licence="all rights reserved",
    claims_supported=["exposed_assets.test-plant.status"],
    peer_reviewed=False,
)


def fictional_spec(capacity_refs: list[str] | None = None) -> AoiSpec:
    return AoiSpec(
        id="test-aoi",
        name="Fictional test AOI",
        countries=("XX",),
        epsg=32631,
        source_zone_bbox=(-0.01, 0.19, 0.01, 0.21),
        river_names=("Test River",),
        overpass_query="[out:json];way(0,0,1,1);out geom;",
        downstream_target=(0.0, -0.2),
        chainage_km=60.0,
        fixture_path="tests/fixtures/synthetic/test-aoi_overpass.json",
        fixture_retrieved_utc=NOW,
        transects=(
            TransectSpec("test-mid", "Test Mid", osm_node_id=10),
            TransectSpec("test-end", "Test End", osm_node_id=11),
        ),
        assets=(
            AssetSpec(
                id="test-plant",
                name="Fictional plant",
                asset_type=AssetType.hydropower_plant,
                status=AssetStatus.operational,
                source_refs=(FICTIONAL_SOURCE.id, FICTIONAL_PRESS.id),
                stated_location=StatedLocation(0.003, 0.05, 250.0, "stated by the fictional page"),
                capacity_mw=Range(
                    low=1.0,
                    high=1.0,
                    best=1.0,
                    unit="MW",
                    source_refs=capacity_refs or [FICTIONAL_SOURCE.id],
                ),
            ),
            AssetSpec(
                id="test-town",
                name="Test End",
                asset_type=AssetType.settlement,
                status=AssetStatus.unknown,
                source_refs=(),
                osm_node_id=11,
            ),
        ),
        sources=(FICTIONAL_SOURCE, FICTIONAL_PRESS),
        extent_source_refs=(),
        notes="Fictional test AOI; nothing here is real.",
        record_created_utc=NOW,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    built = build_aoi(fictional_spec(), DictOverpassClient(FICTIONAL_OVERPASS))
    write_aoi_dir(built, tmp_path / "data" / "aoi" / "test-aoi")
    return tmp_path


def _failed(result: object) -> set[str]:
    return {c.name for c in result.checks if c.failed}  # type: ignore[attr-defined]


def test_fictional_aoi_passes(repo: Path) -> None:
    result = run_suite(repo)
    assert result.passed, _failed(result)
    names = {c.name for c in result.checks}
    assert "test-aoi:grid:recomputed" in names
    assert "test-aoi:centreline:monotonic_chainage" in names
    warn = next(c for c in result.checks if c.name == "test-aoi:hand_digitised_geometry")
    assert warn.severity is Severity.warning and not warn.ok
    assert "test-plant" in warn.details and "source_zone" in warn.details


def test_empty_repo_fails_presence_check(tmp_path: Path) -> None:
    result = run_suite(tmp_path)
    assert not result.passed
    assert _failed(result) == {"aoi_directories_present"}


def test_missing_file_fails_presence(repo: Path) -> None:
    (repo / "data" / "aoi" / "test-aoi" / "grid.json").unlink()
    assert _failed(run_suite(repo)) == {"test-aoi:files_present"}


def test_tampered_grid_fails_recompute(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "grid.json"
    grid = json.loads(path.read_text())
    grid["x_min"] -= 30.0
    grid["x_max"] -= 30.0
    path.write_text(json.dumps(grid))
    assert _failed(run_suite(repo)) == {"test-aoi:grid:recomputed"}


def test_unknown_source_ref_fails_provenance(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "corridor.geojson"
    doc = json.loads(path.read_text())
    doc["features"][0]["properties"]["source_refs"] = ["not-a-source"]
    path.write_text(json.dumps(doc))
    assert _failed(run_suite(repo)) == {"test-aoi:corridor.geojson:provenance"}


def test_empty_source_refs_fails_provenance(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "source_zone.geojson"
    doc = json.loads(path.read_text())
    doc["features"][0]["properties"]["source_refs"] = []
    path.write_text(json.dumps(doc))
    assert _failed(run_suite(repo)) == {"test-aoi:source_zone.geojson:provenance"}


def test_best_on_press_only_capacity_fails(tmp_path: Path) -> None:
    built = build_aoi(fictional_spec([FICTIONAL_PRESS.id]), DictOverpassClient(FICTIONAL_OVERPASS))
    write_aoi_dir(built, tmp_path / "data" / "aoi" / "test-aoi")
    assert _failed(run_suite(tmp_path)) == {"test-aoi:assets:best_has_qualifying_source"}


def test_duplicate_transect_id_fails(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "transects.geojson"
    doc = json.loads(path.read_text())
    doc["features"].append(json.loads(json.dumps(doc["features"][0])))
    path.write_text(json.dumps(doc))
    failed = _failed(run_suite(repo))
    assert "test-aoi:transects:unique_ids" in failed


def test_moved_transect_fails_on_centreline(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "transects.geojson"
    doc = json.loads(path.read_text())
    doc["features"][0]["geometry"]["coordinates"] = [0.02, 0.10]
    path.write_text(json.dumps(doc))
    failed = _failed(run_suite(repo))
    assert "test-aoi:transects:on_centreline" in failed


def test_broken_centreline_fails_length_and_monotonic(repo: Path) -> None:
    path = repo / "data" / "aoi" / "test-aoi" / "river_centreline.geojson"
    doc = json.loads(path.read_text())
    coords = doc["features"][0]["geometry"]["coordinates"]
    coords.insert(1, list(coords[0]))  # repeated vertex -> zero-length step
    doc["features"][0]["properties"]["chainage_km"]["end"] += 5.0
    path.write_text(json.dumps(doc))
    failed = _failed(run_suite(repo))
    assert {
        "test-aoi:centreline:monotonic_chainage",
        "test-aoi:centreline:length_matches_chainage",
    } <= failed


def test_directory_name_must_match_id(repo: Path) -> None:
    src = repo / "data" / "aoi" / "test-aoi"
    shutil.move(str(src), str(repo / "data" / "aoi" / "other-aoi"))
    assert "other-aoi:id_matches_directory" in _failed(run_suite(repo))
