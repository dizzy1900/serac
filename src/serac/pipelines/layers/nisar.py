"""`nisar_hh_t`: HH backscatter from NISAR L2 GCOV granules (placeholder until data exist).

No NISAR granule has been fetched (RELEASE_STATUS.md Known gaps 1), so in every cube built
so far this layer is `build_empty`: all-NaN, `status: not_fetched`. The reader below targets
the GCOV HDF5 layout published in the NISAR product specification
(`/science/LSAR/GCOV/grids/frequencyA/{HHHH, xCoordinates, yCoordinates, projection}`); it
has never been exercised against a real granule and needs `h5py`, which the locked
environment does not ship. Both facts are stated in the layer attrs when the reader runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import reproject

from serac.domain.geo import GridSpec
from serac.domain.manifest import DataSource, ManifestEntry
from serac.pipelines.grid import to_affine
from serac.pipelines.layers._base import (
    coverage_fraction,
    empty_attrs,
    empty_temporal,
    entry_time,
    latest_retrieved,
    layer_attrs,
    licence_of,
    provenance_of,
    resolve_path,
    temporal_array,
    usable,
)

GCOV_GROUP = "/science/LSAR/GCOV/grids/frequencyA"
GCOV_HH = "HHHH"
GCOV_X = "xCoordinates"
GCOV_Y = "yCoordinates"
GCOV_PROJECTION = "projection"
PROCESSING = (
    "NISAR L2 GCOV HHHH (gamma0 backscatter, linear power) read from the granule HDF5 and warped "
    "to the grid by averaging; reader written to the product specification, untested against a "
    "real granule (none fetched as of 2026-09-03)"
)


def read_gcov_hh(path: Path) -> tuple[np.ndarray, Affine, int]:
    """(HHHH array, affine, EPSG) from a GCOV HDF5. Requires h5py; untested on real data."""
    try:
        import h5py  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "reading NISAR GCOV HDF5 needs h5py, which the locked environment does not ship"
        ) from exc
    with h5py.File(path, "r") as fh:
        group = fh[GCOV_GROUP]
        data = np.asarray(group[GCOV_HH][...], dtype=np.float32)
        x = np.asarray(group[GCOV_X][...], dtype=np.float64)
        y = np.asarray(group[GCOV_Y][...], dtype=np.float64)
        proj: Any = group[GCOV_PROJECTION]
        epsg = int(proj.attrs.get("epsg_code", proj[()]))
    dx = float(x[1] - x[0])
    dy = float(y[1] - y[0])
    transform = Affine(dx, 0.0, float(x[0]) - dx / 2, 0.0, dy, float(y[0]) - dy / 2)
    return data, transform, epsg


class NisarHhLayerBuilder:
    name = "nisar_hh_t"
    source = DataSource.nisar_asf

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        rows = [
            e
            for e in usable(entries)
            if e.source is self.source
            and e.path
            and e.path.endswith(".h5")
            and (entry_time(e) is not None)
            and window[0] <= (entry_time(e) or window[0]) <= window[1]
        ]
        rows.sort(key=lambda e: (entry_time(e) or datetime.min, e.product_id))
        if not rows:
            return self.build_empty(grid)
        slices: list[np.ndarray] = []
        times: list[datetime] = []
        coverage: list[float] = []
        for entry in rows:
            data, transform, epsg = read_gcov_hh(resolve_path(self.repo_root, entry))
            dst = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
            reproject(
                source=data,
                destination=dst,
                src_transform=transform,
                src_crs=f"EPSG:{epsg}",
                src_nodata=np.nan,
                dst_transform=to_affine(grid),
                dst_crs=f"EPSG:{grid.epsg}",
                dst_nodata=np.nan,
                resampling=Resampling.average,
            )
            slices.append(dst)
            when = entry_time(entry)
            assert when is not None
            times.append(when)
            coverage.append(coverage_fraction(dst))
        provenance, status = provenance_of(rows)
        levels = sorted({str(e.product_level) for e in rows})
        attrs = layer_attrs(
            source=self.source.value,
            product_ids=[e.product_id for e in rows],
            manifest_entry_ids=[e.entry_id for e in rows],
            retrieved_at=latest_retrieved(rows),
            provenance=provenance,
            status=status,
            licence=licence_of(rows),
            units="1 (gamma0, linear power)",
            processing=PROCESSING,
            native_resolution_m=None,
            coverage_fraction=coverage,
            nisar_levels=levels,
            time_convention="granule start time",
        )
        return temporal_array(grid, np.stack(slices), times, self.name, attrs)

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        return empty_temporal(
            grid, self.name, empty_attrs(self.source.value, "1 (gamma0, linear power)", PROCESSING)
        )
