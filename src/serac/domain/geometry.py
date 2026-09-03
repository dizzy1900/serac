"""Minimal GeoJSON (RFC 7946) geometry contracts.

Only the four geometry types serac stores are modelled. Coordinates are `[lon, lat]` or
`[lon, lat, elevation]` in WGS 84; longitude and latitude are bounds-checked. No shapely or
geopandas here: the domain layer imports only the standard library and pydantic.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from serac.domain.common import DOMAIN_CONFIG

Position = tuple[float, float] | tuple[float, float, float]
"""`(lon, lat)` or `(lon, lat, z)` in EPSG:4326."""


def check_position(pos: Position, where: str) -> None:
    """Raise `ValueError` unless `pos` is a finite WGS 84 position."""
    if not all(math.isfinite(v) for v in pos):
        raise ValueError(f"{where}: coordinates must be finite, got {pos}")
    lon, lat = pos[0], pos[1]
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"{where}: longitude {lon} outside [-180, 180]")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"{where}: latitude {lat} outside [-90, 90]")


def check_ring(ring: list[Position], where: str) -> None:
    """A linear ring has at least four positions and is closed."""
    if len(ring) < 4:
        raise ValueError(f"{where}: a linear ring needs at least 4 positions, got {len(ring)}")
    for i, pos in enumerate(ring):
        check_position(pos, f"{where}[{i}]")
    if ring[0] != ring[-1]:
        raise ValueError(f"{where}: ring is not closed (first != last position)")


class Point(BaseModel):
    """GeoJSON Point."""

    model_config = DOMAIN_CONFIG

    type: Literal["Point"] = "Point"
    coordinates: Position

    @field_validator("coordinates")
    @classmethod
    def _valid(cls, value: Position) -> Position:
        check_position(value, "coordinates")
        return value


class LineString(BaseModel):
    """GeoJSON LineString with at least two positions."""

    model_config = DOMAIN_CONFIG

    type: Literal["LineString"] = "LineString"
    coordinates: list[Position] = Field(min_length=2)

    @field_validator("coordinates")
    @classmethod
    def _valid(cls, value: list[Position]) -> list[Position]:
        for i, pos in enumerate(value):
            check_position(pos, f"coordinates[{i}]")
        return value


class Polygon(BaseModel):
    """GeoJSON Polygon: one exterior ring followed by any number of holes."""

    model_config = DOMAIN_CONFIG

    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[Position]] = Field(min_length=1)

    @field_validator("coordinates")
    @classmethod
    def _valid(cls, value: list[list[Position]]) -> list[list[Position]]:
        for i, ring in enumerate(value):
            check_ring(ring, f"coordinates[{i}]")
        return value


class MultiPolygon(BaseModel):
    """GeoJSON MultiPolygon."""

    model_config = DOMAIN_CONFIG

    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: list[list[list[Position]]] = Field(min_length=1)

    @field_validator("coordinates")
    @classmethod
    def _valid(cls, value: list[list[list[Position]]]) -> list[list[list[Position]]]:
        for p, polygon in enumerate(value):
            if not polygon:
                raise ValueError(f"coordinates[{p}]: polygon has no rings")
            for i, ring in enumerate(polygon):
                check_ring(ring, f"coordinates[{p}][{i}]")
        return value


Geometry = Annotated[Point | LineString | Polygon | MultiPolygon, Field(discriminator="type")]
"""Any supported geometry, discriminated on `type`."""

CONTRACTS: dict[str, type[BaseModel]] = {}
