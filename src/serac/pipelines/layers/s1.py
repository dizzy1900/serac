"""Sentinel-1 InSAR layers from HyP3 products: `s1_coherence_t` and `s1_los_velocity_t`.

One slice per interferometric pair, timed at the secondary acquisition. Coherence comes from
`*_corr.tif`; LOS velocity is `*_los_disp.tif` (metres over the pair) divided by the temporal
baseline in years. Rows labelled `provenance: synthetic` (the only kind in the tree while no
Earthdata credentials exist) make the layer `status: synthetic` and flip the cube's
`contains_synthetic`.
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

CORR_SUFFIX = "_corr.tif"
LOS_SUFFIX = "_los_disp.tif"
DAYS_PER_YEAR = 365.25
COHERENCE_PROCESSING = (
    "HyP3 INSAR_GAMMA coherence (*_corr.tif, 80 m at 20x4 looks) warped to the grid by bilinear "
    "resampling"
)
VELOCITY_PROCESSING = (
    "HyP3 INSAR_GAMMA line-of-sight displacement (*_los_disp.tif, m over the pair) divided by "
    "the temporal baseline in years; warped to the grid by bilinear resampling; positive away "
    "from the satellite as delivered"
)


def pairs(
    entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
) -> list[tuple[str, dict[str, ManifestEntry]]]:
    """pair_id -> {"corr": entry, "los": entry} for pairs whose secondary date is in window."""
    by_pair: dict[str, dict[str, ManifestEntry]] = defaultdict(dict)
    for e in usable(entries):
        if e.source is not DataSource.hyp3_insar or not e.path:
            continue
        when = entry_time(e, prefer_end=True)
        if when is None or not (window[0] <= when <= window[1]):
            continue
        if e.path.endswith(CORR_SUFFIX):
            by_pair[e.product_id]["corr"] = e
        elif e.path.endswith(LOS_SUFFIX):
            by_pair[e.product_id]["los"] = e
    found = [(pid, files) for pid, files in by_pair.items() if "corr" in files]
    return sorted(
        found, key=lambda kv: (entry_time(kv[1]["corr"], prefer_end=True) or datetime.min, kv[0])
    )


def baseline_days(entry: ManifestEntry) -> float | None:
    raw = entry.params.get("dt_days")
    if raw is not None:
        return float(raw)
    if entry.time_start and entry.time_end:
        return (entry.time_end - entry.time_start).total_seconds() / 86_400.0
    return None


class _S1Builder:
    name: str
    key: str
    units: str
    processing: str

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _slice(self, entry: ManifestEntry, grid: GridSpec) -> np.ndarray:
        return reproject_to_grid(
            resolve_path(self.repo_root, entry), grid, resampling=Resampling.bilinear
        )

    def build(
        self, grid: GridSpec, entries: Sequence[ManifestEntry], window: tuple[datetime, datetime]
    ) -> xr.DataArray:
        found = [(pid, files) for pid, files in pairs(entries, window) if self.key in files]
        if not found:
            return self.build_empty(grid)
        slices: list[np.ndarray] = []
        times: list[datetime] = []
        used: list[ManifestEntry] = []
        coverage: list[float] = []
        baselines: list[float | None] = []
        for _pid, files in found:
            entry = files[self.key]
            data = self._slice(entry, grid)
            slices.append(data.astype(np.float32))
            when = entry_time(entry, prefer_end=True)
            assert when is not None
            times.append(when)
            used.append(entry)
            coverage.append(coverage_fraction(data))
            baselines.append(baseline_days(entry))
        provenance, status = provenance_of(used)
        attrs = layer_attrs(
            source=DataSource.hyp3_insar.value,
            product_ids=[pid for pid, _ in found],
            manifest_entry_ids=[e.entry_id for e in used],
            retrieved_at=latest_retrieved(used),
            provenance=provenance,
            status=status,
            licence=licence_of(used),
            units=self.units,
            processing=self.processing,
            native_resolution_m=80.0,
            coverage_fraction=coverage,
            temporal_baseline_days=baselines,
            time_convention="secondary acquisition of the pair",
            notes=used[0].notes if provenance.value == "synthetic" else None,
        )
        return temporal_array(grid, np.stack(slices), times, self.name, attrs)

    def build_empty(self, grid: GridSpec) -> xr.DataArray:
        return empty_temporal(
            grid, self.name, empty_attrs(DataSource.hyp3_insar.value, self.units, self.processing)
        )


class S1CoherenceLayerBuilder(_S1Builder):
    name = "s1_coherence_t"
    key = "corr"
    units = "1"
    processing = COHERENCE_PROCESSING


class S1LosVelocityLayerBuilder(_S1Builder):
    name = "s1_los_velocity_t"
    key = "los"
    units = "m/yr"
    processing = VELOCITY_PROCESSING

    def _slice(self, entry: ManifestEntry, grid: GridSpec) -> np.ndarray:
        disp = super()._slice(entry, grid)
        days = baseline_days(entry)
        if days is None or days <= 0:
            raise ValueError(f"{entry.product_id}: no positive temporal baseline for LOS velocity")
        return disp / (days / DAYS_PER_YEAR)
