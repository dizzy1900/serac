from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from serac.domain import geo
from serac.domain.common import GeometryQuality, Range, RecordMeta, SourceRef
from serac.domain.events import AssetType
from serac.domain.geo import AOI, AssetStatus, ExposedAsset, GridSpec, SlopeUnit, Transect
from serac.domain.geometry import LineString, Point, Polygon

RING = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]
SourceFactory = Callable[..., SourceRef]
RangeFactory = Callable[..., Range]


def _grid(**overrides: Any) -> GridSpec:
    data: dict[str, Any] = {
        "aoi_id": "test-aoi",
        "epsg": 32633,
        "x_min": 300000.0,
        "y_min": 3120000.0,
        "x_max": 300300.0,
        "y_max": 3120600.0,
        "width": 10,
        "height": 20,
    }
    data.update(overrides)
    return GridSpec(**data)


def test_grid_spec_valid_defaults_to_30m() -> None:
    grid = _grid()
    assert grid.resolution_m == 30.0


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"x_min": 300010.0, "x_max": 300310.0}, "x_min=300010.0 is not snapped"),
        ({"y_min": 3120001.0, "y_max": 3120601.0}, "y_min=3120001.0 is not snapped"),
        ({"x_max": 300330.0}, r"x_max=300330.0 != x_min \+ width\*resolution_m \(300300.0\)"),
        ({"y_max": 3120000.0}, r"y_max=3120000.0 != y_min \+ height\*resolution_m"),
        ({"width": 0}, "width"),
        ({"resolution_m": 0}, "resolution_m"),
    ],
)
def test_grid_spec_rejects_inconsistent_extents(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _grid(**overrides)


def test_grid_spec_other_resolution() -> None:
    grid = _grid(resolution_m=10.0, x_max=300100.0, y_max=3120200.0)
    assert grid.width == 10


def _aoi(make_source: SourceFactory, **overrides: Any) -> AOI:
    data: dict[str, Any] = {
        "id": "test-aoi",
        "name": "Fictional AOI",
        "countries": ["ZZ"],
        "cube_epsg": 32633,
        "cube_extent_bbox_4326": (10.0, 20.0, 11.0, 21.0),
        "source_refs": ["test-src-1"],
        "sources": [make_source(claims=["cube_extent_bbox_4326"])],
        "record": RecordMeta(created_utc=make_source().accessed_utc, created_by="test"),
    }
    data.update(overrides)
    return AOI(**data)


def test_aoi_valid_with_grid(make_source: SourceFactory) -> None:
    aoi = _aoi(make_source, grid=_grid())
    assert aoi.grid is not None and aoi.grid.epsg == aoi.cube_epsg


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"countries": ["Atlantis"]}, r"countries\[0\]='Atlantis' is not an ISO 3166-1"),
        ({"countries": ["np"]}, r"countries\[0\]='np'"),
        ({"countries": []}, "countries"),
        ({"cube_extent_bbox_4326": (11.0, 20.0, 10.0, 21.0)}, "cube_extent_bbox_4326: must be"),
        ({"cube_extent_bbox_4326": (10.0, 21.0, 11.0, 20.0)}, "cube_extent_bbox_4326: must be"),
        ({"cube_extent_bbox_4326": (float("nan"), 20.0, 11.0, 21.0)}, "finite"),
        ({"grid": _grid(aoi_id="test-other")}, "grid.aoi_id='test-other' != id='test-aoi'"),
        ({"grid": _grid(epsg=32632)}, "grid.epsg=32632 != cube_epsg=32633"),
        ({"source_refs": ["test-missing"]}, r"source_refs\[0\]: source 'test-missing'"),
    ],
)
def test_aoi_rejections(make_source: SourceFactory, overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _aoi(make_source, **overrides)


def _slope_unit(**overrides: Any) -> SlopeUnit:
    data: dict[str, Any] = {
        "id": "test-su-1",
        "aoi_id": "test-aoi",
        "geometry": Polygon(coordinates=[RING]),
        "aspect_deg": 90.0,
        "mean_slope_deg": 40.0,
        "elevation_band_m": (5000.0, 6000.0),
        "glacier_cover": True,
        "geometry_quality": GeometryQuality.hand_digitised_approximate,
        "source_refs": ["test-dem-1"],
    }
    data.update(overrides)
    return SlopeUnit(**data)


def test_slope_unit_valid_and_band_checks() -> None:
    su = _slope_unit(permafrost_index=0.5, area_m2=10.0, lithology_tag="fictional")
    assert su.permafrost_index == 0.5
    with pytest.raises(
        ValidationError, match=r"elevation_band_m: low=6000\.0 exceeds high=5000\.0"
    ):
        _slope_unit(elevation_band_m=(6000.0, 5000.0))
    with pytest.raises(ValidationError, match="elevation_band_m: values must be finite"):
        _slope_unit(elevation_band_m=(float("inf"), 5000.0))
    with pytest.raises(ValidationError, match="aspect_deg"):
        _slope_unit(aspect_deg=360.0)
    with pytest.raises(ValidationError, match="permafrost_index"):
        _slope_unit(permafrost_index=1.5)


def test_transect() -> None:
    t = Transect(
        id="test-t",
        aoi_id="test-aoi",
        name="Fictional transect",
        chainage_km=12.5,
        point=Point(coordinates=(10.5, 20.5)),
        cross_section=LineString(coordinates=[(10.49, 20.5), (10.51, 20.5)]),
        geometry_quality=GeometryQuality.snapped_to_osm_centreline,
        positional_accuracy_m=50.0,
        source_refs=["test-src-1"],
    )
    assert t.cross_section is not None
    with pytest.raises(ValidationError, match="chainage_km"):
        Transect(
            id="test-t",
            aoi_id="test-aoi",
            name="x",
            chainage_km=-1.0,
            point=Point(coordinates=(10.5, 20.5)),
            geometry_quality=GeometryQuality.osm_node,
            source_refs=["test-src-1"],
        )


def _asset(make_range: RangeFactory, **overrides: Any) -> ExposedAsset:
    data: dict[str, Any] = {
        "id": "test-asset-1",
        "aoi_id": "test-aoi",
        "name": "Fictional plant",
        "asset_type": AssetType.hydropower_plant,
        "status": AssetStatus.operational,
        "geometry": Point(coordinates=(10.5, 20.5)),
        "capacity_mw": make_range(unit="MW"),
        "geometry_quality": GeometryQuality.osm_node,
        "source_refs": ["test-src-1"],
    }
    data.update(overrides)
    return ExposedAsset(**data)


def test_exposed_asset_hydropower(make_range: RangeFactory) -> None:
    asset = _asset(make_range)
    assert asset.capacity_mw is not None
    with pytest.raises(ValidationError, match=r"capacity_mw\.unit must be 'MW'"):
        _asset(make_range, capacity_mw=make_range(unit="kW"))
    with pytest.raises(ValidationError, match="capacity_mw: only valid for hydropower_plant"):
        _asset(make_range, asset_type=AssetType.bridge)


def test_exposed_asset_settlement(make_range: RangeFactory) -> None:
    village = _asset(
        make_range,
        asset_type=AssetType.settlement,
        status=AssetStatus.evacuated,
        capacity_mw=None,
        population=make_range(unit="persons"),
        geometry=Polygon(coordinates=[RING]),
    )
    assert village.population is not None
    with pytest.raises(ValidationError, match=r"population\.unit must be 'persons'"):
        _asset(
            make_range, asset_type=AssetType.settlement, capacity_mw=None, population=make_range()
        )
    with pytest.raises(ValidationError, match="population: only valid for settlement"):
        _asset(make_range, population=make_range(unit="persons"))


def test_exposed_asset_geometry_discriminator(make_range: RangeFactory) -> None:
    asset = _asset(make_range, geometry={"type": "Point", "coordinates": [10.5, 20.5]})
    assert isinstance(asset.geometry, Point)
    with pytest.raises(ValidationError, match="type"):
        _asset(make_range, geometry={"type": "Nope", "coordinates": [10.5, 20.5]})


def test_contracts_table() -> None:
    assert set(geo.CONTRACTS) == {"aoi", "grid-spec", "slope-unit", "transect", "exposed-asset"}
