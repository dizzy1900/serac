"""The AOI pipeline on the committed Overpass fixtures (fake client, no network)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform

from serac.domain.common import GeometryQuality, SourceKind
from serac.domain.geo import GridSpec
from serac.pipelines.aoi_build import (
    AoiBuildError,
    BuiltAoi,
    FixtureOverpassClient,
    RiverNetwork,
    build_aoi,
    build_centreline,
    grid_from_bbox,
    parse_overpass,
    read_aoi_dir,
    transformers,
    write_aoi_dir,
)
from serac.pipelines.aoi_specs import AOI_SPECS, LHENDE_KHOLA_TRISHULI


@pytest.fixture(scope="module")
def lhende(repo_root: Path) -> BuiltAoi:
    spec = LHENDE_KHOLA_TRISHULI
    client = FixtureOverpassClient(repo_root / spec.fixture_path)
    built = build_aoi(spec, client)
    assert client.queries == [spec.overpass_query]
    return built


def test_fixture_parses_with_relation_members(repo_root: Path) -> None:
    data = parse_overpass((repo_root / LHENDE_KHOLA_TRISHULI.fixture_path).read_bytes())
    assert data.timestamp_osm_base and data.timestamp_osm_base.startswith("2026-09-03")
    assert data.copyright and "ODbL" in data.copyright
    assert data.relation_count >= 4
    # relation member ways without their own tags are still usable as waterways
    member_only = [w for w in data.ways.values() if "osm:relation" in w.tags]
    assert member_only, "expected relation members with geometry"
    assert any(w.tags.get("waterway") for w in member_only)
    assert len(data.waterway_ways()) > 50


def test_parse_rejects_html_error_page() -> None:
    with pytest.raises(AoiBuildError, match="not JSON"):
        parse_overpass(b"<html>Error: runtime error: ... too busy</html>")


def test_network_merges_into_single_oriented_path(repo_root: Path) -> None:
    spec = LHENDE_KHOLA_TRISHULI
    data = parse_overpass((repo_root / spec.fixture_path).read_bytes())
    to_proj, _ = transformers(spec.epsg)
    net = RiverNetwork(data.waterway_ways(), to_proj)
    zone = transform(to_proj.transform, box(*spec.source_zone_bbox))
    target = Point(*to_proj.transform(*spec.downstream_target))
    result = build_centreline(net, zone, target, spec.chainage_km * 1000.0)
    line = result.line_proj
    assert line.geom_type == "LineString"
    assert result.clipped and math.isclose(line.length, 100_000.0, rel_tol=1e-6)
    assert result.full_length_m > 100_000.0
    # oriented downstream: the start is closer to the source zone than the end
    start, end = Point(line.coords[0]), Point(line.coords[-1])
    assert start.distance(zone) < end.distance(zone)
    assert start.distance(zone) < 6_000.0, "chainage 0 should be within a few km of the zone"
    # chainage is monotonic: no zero-length steps
    steps = [math.dist(line.coords[i], line.coords[i + 1]) for i in range(len(line.coords) - 1)]
    assert min(steps) > 0.0
    # the path runs Lhende Khola -> Bhote Koshi -> Trishuli
    assert {937405875, 201928141, 809865767, 24624604} <= set(result.way_ids)


def test_transect_chainages_increase_downstream(lhende: BuiltAoi) -> None:
    ch = lhende.report.transect_chainage_km
    order = ["rasuwagadhi-gyirong", "syabrubesi", "betrawati", "galchhi"]
    assert list(ch) == order
    assert ch["rasuwagadhi-gyirong"] < ch["syabrubesi"] < ch["betrawati"] < ch["galchhi"] < 100.0
    for t in lhende.transects:
        assert t.geometry_quality is GeometryQuality.snapped_to_osm_centreline
        assert t.positional_accuracy_m is not None and t.positional_accuracy_m >= 50.0
    # snapped points lie on the centreline
    to_proj, _ = transformers(lhende.spec.epsg)
    line = transform(to_proj.transform, lhende.centreline)
    for t in lhende.transects:
        p = transform(to_proj.transform, Point(t.point.coordinates))
        assert p.distance(line) < 1.0


def test_corridor_buffer_area_is_sane(lhende: BuiltAoi) -> None:
    to_proj, _ = transformers(lhende.spec.epsg)
    corridor = transform(to_proj.transform, lhende.corridor)
    line = transform(to_proj.transform, lhende.centreline)
    width = 2 * lhende.spec.corridor_buffer_m
    # a buffer of a meandering line is smaller than 2*w*L (overlaps) but not by much
    assert 0.5 * width * line.length < corridor.area <= width * line.length + math.pi * 1500**2
    assert corridor.contains(line)
    assert shape(json.loads(json.dumps(corridor.__geo_interface__))).is_valid


def test_grid_is_snapped_and_recomputable(lhende: BuiltAoi) -> None:
    grid = lhende.aoi.grid
    assert isinstance(grid, GridSpec)
    assert grid.resolution_m == 30.0
    assert grid.x_min % 30 == 0 and grid.y_min % 30 == 0
    assert grid == grid_from_bbox(
        lhende.aoi.id, lhende.aoi.cube_epsg, lhende.aoi.cube_extent_bbox_4326
    )
    # the grid covers the extent bbox with at most one pixel of slack per side
    to_proj = Transformer.from_crs(4326, lhende.aoi.cube_epsg, always_xy=True)
    w, s, e, n = lhende.aoi.cube_extent_bbox_4326
    for lon, lat in ((w, s), (w, n), (e, s), (e, n)):
        x, y = to_proj.transform(lon, lat)
        assert grid.x_min - 30 <= x <= grid.x_max + 30
        assert grid.y_min - 30 <= y <= grid.y_max + 30
    # 100 km corridor + source zone: thousands of pixels, not millions
    assert 1000 < grid.width < 5000 and 1000 < grid.height < 5000


def test_extent_contains_source_zone_and_corridor(lhende: BuiltAoi) -> None:
    extent = box(*lhende.aoi.cube_extent_bbox_4326)
    assert extent.contains(lhende.source_zone)
    assert extent.contains(lhende.corridor)


def test_osm_source_ref_hashes_the_fixture(lhende: BuiltAoi, repo_root: Path) -> None:
    import hashlib

    osm = next(s for s in lhende.aoi.sources if s.id == lhende.spec.osm_source_id)
    assert osm.kind is SourceKind.dataset and osm.licence == "ODbL-1.0"
    assert osm.stored_copy == lhende.spec.fixture_path
    assert osm.sha256 == hashlib.sha256((repo_root / osm.stored_copy).read_bytes()).hexdigest()
    assert osm.accessed_utc == lhende.spec.fixture_retrieved_utc


def test_assets_are_located_and_linked(lhende: BuiltAoi) -> None:
    by_id = {a.id: a for a in lhende.assets}
    assert by_id["rasuwagadhi-hep"].geometry_quality is GeometryQuality.osm_way_centroid
    assert by_id["rasuwagadhi-hep"].transect_id == "rasuwagadhi-gyirong"
    assert by_id["sanjen-hep"].geometry_quality is GeometryQuality.source_stated_location
    assert by_id["sanjen-hep"].transect_id is None  # tributary, > 5 km from the centreline
    assert lhende.asset_accuracy_m["sanjen-hep"] == 3000.0
    # settlements cite the OSM dataset automatically
    assert by_id["timure"].source_refs == [lhende.spec.osm_source_id]
    for a in lhende.assets:
        assert a.capacity_mw is None or a.capacity_mw.unit == "MW"
    assert len(by_id) == len(lhende.spec.assets)


def test_write_and_read_roundtrip(lhende: BuiltAoi, tmp_path: Path) -> None:
    written = write_aoi_dir(lhende, tmp_path / lhende.aoi.id)
    assert {p.name for p in written} == {
        "aoi.json",
        "grid.json",
        "source_zone.geojson",
        "river_centreline.geojson",
        "corridor.geojson",
        "transects.geojson",
        "exposed_assets.geojson",
    }
    files = read_aoi_dir(tmp_path / lhende.aoi.id)
    assert files.aoi == lhende.aoi
    assert files.grid == lhende.aoi.grid
    assert files.transects == lhende.transects
    assert files.assets == lhende.assets
    for name in ("source_zone.geojson", "river_centreline.geojson", "corridor.geojson"):
        doc = json.loads((tmp_path / lhende.aoi.id / name).read_text())
        assert doc["type"] == "FeatureCollection" and "crs" not in doc
        props = doc["features"][0]["properties"]
        assert props["source_refs"] and props["geometry_quality"] and "feature_type" in props
    ring = json.loads((tmp_path / lhende.aoi.id / "corridor.geojson").read_text())
    poly = Polygon(ring["features"][0]["geometry"]["coordinates"][0])
    assert poly.exterior.is_ccw  # RFC 7946 right-hand rule


@pytest.mark.parametrize("aoi_id", sorted(AOI_SPECS))
def test_every_spec_builds_from_its_fixture(aoi_id: str, repo_root: Path) -> None:
    spec = AOI_SPECS[aoi_id]
    built = build_aoi(spec, FixtureOverpassClient(repo_root / spec.fixture_path))
    assert built.aoi.id == aoi_id
    assert [t.id for t in built.transects] == [t.id for t in spec.transects]
    assert built.report.centreline_length_km > 5.0
    assert built.report.start_offset_m < 10_000.0
