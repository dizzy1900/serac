"""Areas of interest and the vector features that live in them.

An `AOI` directory holds `aoi.json` plus GeoJSON feature collections (transects, exposed
assets, slope units). Every feature carries `source_refs` and a `geometry_quality` so that
validation and the release ledger can say how each geometry was obtained.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from serac.domain.common import (
    DOMAIN_CONFIG,
    GeometryQuality,
    Range,
    RecordMeta,
    Slug,
    SourceRef,
)
from serac.domain.events import AssetType
from serac.domain.geometry import Geometry, LineString, MultiPolygon, Point, Polygon

Bbox4326 = tuple[float, float, float, float]
"""`(west, south, east, north)` in degrees."""


def check_bbox_4326(bbox: Bbox4326, where: str = "bbox_4326") -> None:
    """Raise `ValueError` unless `bbox` is an ordered, finite WGS 84 box."""
    if not all(math.isfinite(v) for v in bbox):
        raise ValueError(f"{where}: values must be finite")
    w, s, e, n = bbox
    if not (-180.0 <= w <= e <= 180.0 and -90.0 <= s <= n <= 90.0):
        raise ValueError(
            f"{where}: must be (west, south, east, north) with west<=east, south<=north"
        )


class GridSpec(BaseModel):
    """The fixed raster grid of an AOI's feature cube (projected CRS, pixel edges).

    `x_min/y_min` are the outer edges of the lower-left pixel and must be snapped to
    `resolution_m`; `x_max/y_max` follow from `width/height`.
    """

    model_config = DOMAIN_CONFIG

    aoi_id: Slug
    epsg: int = Field(ge=1024, le=32767)
    resolution_m: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    x_min: float = Field(allow_inf_nan=False)
    y_min: float = Field(allow_inf_nan=False)
    x_max: float = Field(allow_inf_nan=False)
    y_max: float = Field(allow_inf_nan=False)
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        tol = 1e-6 * self.resolution_m
        for name, value in (("x_min", self.x_min), ("y_min", self.y_min)):
            remainder = math.remainder(value, self.resolution_m)
            if abs(remainder) > tol:
                problems.append(
                    f"{name}={value} is not snapped to resolution_m={self.resolution_m}"
                )
        expected_x = self.x_min + self.width * self.resolution_m
        if abs(expected_x - self.x_max) > tol:
            problems.append(f"x_max={self.x_max} != x_min + width*resolution_m ({expected_x})")
        expected_y = self.y_min + self.height * self.resolution_m
        if abs(expected_y - self.y_max) > tol:
            problems.append(f"y_max={self.y_max} != y_min + height*resolution_m ({expected_y})")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class AOI(BaseModel):
    """An area of interest: a source zone plus a downstream corridor."""

    model_config = DOMAIN_CONFIG

    id: Slug
    name: str = Field(min_length=1)
    countries: list[str] = Field(min_length=1, description="ISO 3166-1 alpha-2 codes")
    cube_epsg: int = Field(ge=1024, le=32767)
    cube_extent_bbox_4326: Bbox4326
    grid: GridSpec | None = None
    river_names: list[str] = Field(default_factory=list)
    source_refs: list[Slug] = Field(min_length=1, description="Sources for the extents")
    sources: list[SourceRef] = Field(min_length=1)
    notes: str | None = Field(
        default=None, description="Also lists assets named in reporting but not yet sourced"
    )
    record: RecordMeta

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        for i, code in enumerate(self.countries):
            if not (len(code) == 2 and code.isalpha() and code.isupper()):
                problems.append(f"countries[{i}]={code!r} is not an ISO 3166-1 alpha-2 code")
        try:
            check_bbox_4326(self.cube_extent_bbox_4326, "cube_extent_bbox_4326")
        except ValueError as exc:
            problems.append(str(exc))
        if self.grid is not None:
            if self.grid.aoi_id != self.id:
                problems.append(f"grid.aoi_id={self.grid.aoi_id!r} != id={self.id!r}")
            if self.grid.epsg != self.cube_epsg:
                problems.append(f"grid.epsg={self.grid.epsg} != cube_epsg={self.cube_epsg}")
        ids = {s.id for s in self.sources}
        for i, ref in enumerate(self.source_refs):
            if ref not in ids:
                problems.append(f"source_refs[{i}]: source {ref!r} is not in sources[]")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class SlopeUnit(BaseModel):
    """A terrain unit of the L0 susceptibility inventory (derived from a DEM, so plain floats)."""

    model_config = DOMAIN_CONFIG

    id: Slug
    aoi_id: Slug
    geometry: Polygon | MultiPolygon
    aspect_deg: float = Field(ge=0, lt=360, allow_inf_nan=False)
    mean_slope_deg: float = Field(ge=0, le=90, allow_inf_nan=False)
    elevation_band_m: tuple[float, float]
    lithology_tag: str | None = None
    glacier_cover: bool
    permafrost_index: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    area_m2: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    geometry_quality: GeometryQuality
    source_refs: list[Slug] = Field(min_length=1, description="DEM / inventory sources")
    notes: str | None = None

    @model_validator(mode="after")
    def _band(self) -> Self:
        low, high = self.elevation_band_m
        if not (math.isfinite(low) and math.isfinite(high)):
            raise ValueError("elevation_band_m: values must be finite")
        if low > high:
            raise ValueError(f"elevation_band_m: low={low} exceeds high={high}")
        return self


class Transect(BaseModel):
    """A river cross-section where arrival times and stages are observed or forecast."""

    model_config = DOMAIN_CONFIG

    id: Slug
    aoi_id: Slug
    name: str = Field(min_length=1)
    chainage_km: float = Field(ge=0, allow_inf_nan=False, description="Along the centreline")
    point: Point
    cross_section: LineString | None = None
    geometry_quality: GeometryQuality
    positional_accuracy_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_refs: list[Slug] = Field(min_length=1)
    notes: str | None = None


class AssetStatus(StrEnum):
    operational = "operational"
    under_construction = "under_construction"
    damaged = "damaged"
    destroyed = "destroyed"
    evacuated = "evacuated"
    decommissioned = "decommissioned"
    unknown = "unknown"


class ExposedAsset(BaseModel):
    """Something in the corridor that a cascade can reach."""

    model_config = DOMAIN_CONFIG

    id: Slug
    aoi_id: Slug
    name: str = Field(min_length=1)
    asset_type: AssetType
    status: AssetStatus
    geometry: Geometry
    capacity_mw: Range | None = Field(default=None, description="Hydropower plants only")
    population: Range | None = Field(default=None, description="Settlements only")
    transect_id: Slug | None = Field(default=None, description="Nearest transect, if any")
    geometry_quality: GeometryQuality
    positional_accuracy_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_refs: list[Slug] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        problems: list[str] = []
        if self.capacity_mw is not None:
            if self.asset_type != AssetType.hydropower_plant:
                problems.append(
                    f"capacity_mw: only valid for hydropower_plant, not {self.asset_type}"
                )
            if self.capacity_mw.unit != "MW":
                problems.append(f"capacity_mw.unit must be 'MW', got {self.capacity_mw.unit!r}")
        if self.population is not None:
            if self.asset_type != AssetType.settlement:
                problems.append(f"population: only valid for settlement, not {self.asset_type}")
            if self.population.unit != "persons":
                problems.append(f"population.unit must be 'persons', got {self.population.unit!r}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


CONTRACTS: dict[str, type[BaseModel]] = {
    "aoi": AOI,
    "grid-spec": GridSpec,
    "slope-unit": SlopeUnit,
    "transect": Transect,
    "exposed-asset": ExposedAsset,
}
