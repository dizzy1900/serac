"""Port for elevation providers.

`Glo30DemAdapter` is the open default (Copernicus GLO-30). Licensed higher-resolution DEMs
(e.g. national LiDAR, commercial WorldDEM) plug in by implementing `DemProvider`; the cube
builder only ever talks to this port.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from serac.domain.manifest import DataSource

Bbox4326 = tuple[float, float, float, float]
AffineCoefficients = tuple[float, float, float, float, float, float]
"""GDAL/rasterio affine (a, b, c, d, e, f): x = a*col + b*row + c; y = d*col + e*row + f."""


@dataclass(frozen=True)
class DemWindow:
    """An elevation array plus the georeferencing to place it, as delivered (no reprojection)."""

    data: NDArray[np.float32]
    transform: AffineCoefficients
    crs: str
    nodata: float | None
    source: DataSource
    product_ids: tuple[str, ...]
    units: str = "m"

    @property
    def shape(self) -> tuple[int, int]:
        rows, cols = self.data.shape
        return rows, cols

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) of the pixel edges in `crs` units (north-up rasters)."""
        a, _b, c, _d, e, f = self.transform
        rows, cols = self.shape
        return c, f + e * rows, c + a * cols, f


class DemProvider(ABC):
    """Anything that can serve an elevation window for a WGS84 bbox."""

    provider_name: ClassVar[str]
    native_resolution_m: ClassVar[float]
    licence: ClassVar[str]

    @abstractmethod
    def read_window(self, bbox_4326: Bbox4326, *, buffer_m: float = 0.0) -> DemWindow:
        """Return the DEM covering `bbox_4326` expanded by `buffer_m` metres on every side."""
