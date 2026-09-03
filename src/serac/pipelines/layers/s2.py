"""Sentinel-2 layers: `s2_ndsi_t` (float32) and `s2_cloud_t` (uint8 SCL class) per scene.

A scene contributes when its B03, B11 and SCL windows are all in the ledger (Earth Search or
CDSE rows share the same file layout). Bands are warped to the grid: B03/B11 by averaging
(10/20 m -> 30 m), SCL by mode. NDSI = (B03 - B11) / (B03 + B11); pixels where either band is
0 (nodata) or the sum is 0 are NaN. The slice time is the scene's acquisition instant.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy as np
import xarray as xr
from rasterio.enums import Resampling

from serac.domain.geo import GridSpec
from serac.domain.manifest import DataSource, ManifestEntry
from serac.pipelines.layers._base import (
    coverage_fraction,
    empty_attrs,
    empty_temporal,
    entry_time,
    latest_retrieved,
    layer_attrs,
    licence_of,
    provenance_of,
    reproject_to_grid,
    resolve_path,
    temporal_array,
    usable,
)

S2_SOURCES: frozenset[DataSource] = frozenset(
    {DataSource.sentinel2_earthsearch, DataSource.sentinel2_cdse}
)
BANDS = ("B03", "B11", "SCL")
NDSI_PROCESSING = (
    "B03 (10 m) and B11 (20 m) L2A surface reflectance windows warped to the grid by averaging; "
    "NDSI = (B03 - B11) / (B03 + B11); NaN where a band is nodata (0) or the sum is 0"
)
CLOUD_PROCESSING = (
    "L2A Scene Classification Layer (20 m) warped to the grid by mode; values are SCL classes "
    "0-11; 255 outside the fetched window"
)
SCL_FILL = 255


def scenes(
    entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
) -> dict[str, dict[str, ManifestEntry]]:
    """product_id -> {band: entry} for scenes with all three bands inside the window."""
    by_scene: dict[str, dict[str, ManifestEntry]] = defaultdict(dict)
    for e in usable(entries):
        if e.source not in S2_SOURCES or e.params.get("kind") != "band":
            continue
        when = entry_time(e)
        if when is None or not (window[0] <= when <= window[1]):
            continue
        band = str(e.params.get("band"))
        if band in BANDS:
            prev = by_scene[e.product_id].get(band)
            if prev is None or e.recorded_at >= prev.recorded_at:
                by_scene[e.product_id][band] = e
    return {pid: bands for pid, bands in by_scene.items() if all(b in bands for b in BANDS)}


def _ordered(
    found: dict[str, dict[str, ManifestEntry]],
) -> list[tuple[str, dict[str, ManifestEntry]]]:
    return sorted(found.items(), key=lambda kv: (entry_time(kv[1]["SCL"]) or datetime.min, kv[0]))


class S2NdsiLayerBuilder:
    name = "s2_ndsi_t"

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        found = _ordered(scenes(entries, window))
        if not found:
            return self.build_empty(grid)
        slices: list[np.ndarray] = []
        times: list[datetime] = []
        used: list[ManifestEntry] = []
        coverage: list[float] = []
        for _pid, bands in found:
            b03 = reproject_to_grid(
                resolve_path(self.repo_root, bands["B03"]),
                grid,
                resampling=Resampling.average,
                src_nodata=0,
            )
            b11 = reproject_to_grid(
                resolve_path(self.repo_root, bands["B11"]),
                grid,
                resampling=Resampling.average,
                src_nodata=0,
            )
            total = b03 + b11
            with np.errstate(invalid="ignore", divide="ignore"):
                ndsi = np.where((total > 0) & (b03 > 0) & (b11 > 0), (b03 - b11) / total, np.nan)
            slices.append(ndsi.astype(np.float32))
            when = entry_time(bands["SCL"])
            assert when is not None
            times.append(when)
            used.extend(bands.values())
            coverage.append(coverage_fraction(ndsi))
        provenance, status = provenance_of(used)
        attrs = layer_attrs(
            source=",".join(sorted({e.source.value for e in used})),
            product_ids=[pid for pid, _ in found],
            manifest_entry_ids=[e.entry_id for e in used],
            retrieved_at=latest_retrieved(used),
            provenance=provenance,
            status=status,
            licence=licence_of(used),
            units="1",
            processing=NDSI_PROCESSING,
            native_resolution_m=10.0,
            coverage_fraction=coverage,
            time_convention="acquisition instant of the scene",
        )
        return temporal_array(grid, np.stack(slices), times, self.name, attrs)

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        return empty_temporal(grid, self.name, empty_attrs("sentinel2", "1", NDSI_PROCESSING))


class S2CloudLayerBuilder:
    name = "s2_cloud_t"

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        found = _ordered(scenes(entries, window))
        if not found:
            return self.build_empty(grid)
        slices: list[np.ndarray] = []
        times: list[datetime] = []
        used: list[ManifestEntry] = []
        coverage: list[float] = []
        for _pid, bands in found:
            scl = reproject_to_grid(
                resolve_path(self.repo_root, bands["SCL"]),
                grid,
                resampling=Resampling.mode,
                dtype="uint8",
                dst_nodata=SCL_FILL,
                src_nodata=SCL_FILL,  # keep class 0 (no data inside the scene) as a class
            )
            slices.append(scl)
            when = entry_time(bands["SCL"])
            assert when is not None
            times.append(when)
            used.append(bands["SCL"])
            coverage.append(coverage_fraction(scl))
        provenance, status = provenance_of(used)
        attrs = layer_attrs(
            source=",".join(sorted({e.source.value for e in used})),
            product_ids=[pid for pid, _ in found],
            manifest_entry_ids=[e.entry_id for e in used],
            retrieved_at=latest_retrieved(used),
            provenance=provenance,
            status=status,
            licence=licence_of(used),
            units="SCL class",
            processing=CLOUD_PROCESSING,
            native_resolution_m=20.0,
            coverage_fraction=coverage,
            time_convention="acquisition instant of the scene",
            scl_legend={
                "0": "no_data",
                "1": "saturated_or_defective",
                "2": "dark_area",
                "3": "cloud_shadow",
                "4": "vegetation",
                "5": "not_vegetated",
                "6": "water",
                "7": "unclassified",
                "8": "cloud_medium_probability",
                "9": "cloud_high_probability",
                "10": "thin_cirrus",
                "11": "snow_or_ice",
                "255": "outside fetched window",
            },
        )
        return temporal_array(grid, np.stack(slices), times, self.name, attrs)

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        empty = empty_temporal(
            grid, self.name, empty_attrs("sentinel2", "SCL class", CLOUD_PROCESSING)
        )
        return empty.astype(np.uint8)
