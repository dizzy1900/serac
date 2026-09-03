from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from serac.domain.geometry import Geometry, LineString, MultiPolygon, Point, Polygon

RING = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]


def test_point_accepts_2d_and_3d() -> None:
    assert Point(coordinates=(10.0, 20.0)).coordinates == (10.0, 20.0)
    assert Point(coordinates=(10.0, 20.0, 3000.0)).coordinates == (10.0, 20.0, 3000.0)
    assert Point(coordinates=(10.0, 20.0)).type == "Point"


@pytest.mark.parametrize(
    ("coords", "match"),
    [
        ((181.0, 0.0), r"longitude 181.0 outside"),
        ((0.0, -91.0), r"latitude -91.0 outside"),
        ((math.nan, 0.0), "finite"),
        ((0.0, math.inf), "finite"),
    ],
)
def test_point_bounds(coords: tuple[float, float], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Point(coordinates=coords)


def test_point_rejects_wrong_arity_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Point.model_validate({"coordinates": [1.0]})
    with pytest.raises(ValidationError, match="extra"):
        Point.model_validate({"coordinates": [1.0, 2.0], "bbox": [0, 0, 1, 1]})


def test_linestring_needs_two_positions_and_valid_coords() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        LineString(coordinates=[(0.0, 0.0)])
    with pytest.raises(ValidationError, match=r"coordinates\[1\]: latitude 95.0"):
        LineString(coordinates=[(0.0, 0.0), (0.0, 95.0)])
    assert len(LineString(coordinates=[(0.0, 0.0), (1.0, 1.0, 5.0)]).coordinates) == 2


def test_polygon_rings() -> None:
    with pytest.raises(ValidationError, match=r"coordinates\[0\]: a linear ring needs at least 4"):
        Polygon(coordinates=[RING[:3]])
    with pytest.raises(ValidationError, match=r"coordinates\[0\]: ring is not closed"):
        Polygon(coordinates=[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]])
    with pytest.raises(ValidationError, match=r"coordinates\[1\]\[0\]: longitude 200.0"):
        Polygon(coordinates=[RING, [(200.0, 0.0), *RING[1:]]])
    with pytest.raises(ValidationError, match="at least 1"):
        Polygon(coordinates=[])
    assert len(Polygon(coordinates=[RING, RING]).coordinates) == 2


def test_multipolygon() -> None:
    with pytest.raises(ValidationError, match=r"coordinates\[0\]: polygon has no rings"):
        MultiPolygon(coordinates=[[]])
    with pytest.raises(ValidationError, match=r"coordinates\[1\]\[0\]: ring is not closed"):
        MultiPolygon(coordinates=[[RING], [[*RING[:-1], (5.0, 5.0)]]])
    assert MultiPolygon(coordinates=[[RING], [RING]]).type == "MultiPolygon"


def test_geometry_union_discriminates_on_type() -> None:
    adapter: TypeAdapter[Point | LineString | Polygon | MultiPolygon] = TypeAdapter(Geometry)
    geom = adapter.validate_python({"type": "Polygon", "coordinates": [RING]})
    assert isinstance(geom, Polygon)
    point = adapter.validate_json('{"type": "Point", "coordinates": [1.0, 2.0]}')
    assert isinstance(point, Point)
    with pytest.raises(ValidationError, match="type"):
        adapter.validate_python({"type": "Circle", "coordinates": [0.0, 0.0]})
    schema = adapter.json_schema()
    assert schema["discriminator"]["propertyName"] == "type"
