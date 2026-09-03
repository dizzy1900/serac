"""Cloud-aware Sentinel-2 scene selection, shared by the Earth Search and CDSE adapters.

The Scene Classification Layer (SCL) of a Level-2A product classifies every 20 m pixel:

    0 no data, 1 saturated/defective, 2 dark area, 3 cloud shadow, 4 vegetation,
    5 not vegetated, 6 water, 7 unclassified, 8 cloud medium probability,
    9 cloud high probability, 10 thin cirrus, 11 snow/ice

serac treats {3, 8, 9, 10, 11} as "not usable for surface change" over the AOI: cloud shadow,
medium/high-probability cloud, thin cirrus, and snow/ice. Snow is included deliberately:
this project looks for rock-ice detachment signatures, and a snow-flagged pixel is as
uninformative for NDSI-based change as a cloudy one. Callers that want a pure cloud fraction
pass `classes=CLOUD_ONLY_CLASSES`.

Tile-level `eo:cloud_cover` is a fallback ranking signal only; the AOI fraction from an SCL
window is what the selection prefers whenever it is available.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray

SCL_NODATA = 0
CLOUD_SHADOW_SNOW_CLASSES: frozenset[int] = frozenset({3, 8, 9, 10, 11})
CLOUD_ONLY_CLASSES: frozenset[int] = frozenset({3, 8, 9, 10})
SCL_LEGEND: dict[int, str] = {
    0: "no_data",
    1: "saturated_or_defective",
    2: "dark_area",
    3: "cloud_shadow",
    4: "vegetation",
    5: "not_vegetated",
    6: "water",
    7: "unclassified",
    8: "cloud_medium_probability",
    9: "cloud_high_probability",
    10: "thin_cirrus",
    11: "snow_or_ice",
}


def cloud_fraction(
    scl: NDArray[Any],
    *,
    classes: Iterable[int] = CLOUD_SHADOW_SNOW_CLASSES,
    nodata: int = SCL_NODATA,
) -> float | None:
    """Fraction of valid SCL pixels in `classes`; None when no pixel is valid (unknown, not 0)."""
    arr = np.asarray(scl)
    valid = arr != nodata
    n_valid = int(valid.sum())
    if n_valid == 0:
        return None
    flagged = np.isin(arr[valid], np.fromiter(classes, dtype=arr.dtype))
    return float(flagged.sum() / n_valid)


def class_histogram(scl: NDArray[Any]) -> dict[str, int]:
    """Pixel counts per SCL class name, for provenance notes."""
    arr = np.asarray(scl)
    values, counts = np.unique(arr, return_counts=True)
    return {
        SCL_LEGEND.get(int(v), f"class_{int(v)}"): int(c)
        for v, c in zip(values, counts, strict=True)
    }


@dataclass(frozen=True)
class SceneCandidate:
    """What the selector needs to know about one scene."""

    product_id: str
    acquired: datetime
    tile_cloud_cover: float | None = None
    """`eo:cloud_cover`, percent over the whole tile (0-100), when the catalogue reports it."""
    aoi_cloud_fraction: float | None = None
    """Fraction (0-1) of flagged pixels over the AOI window from the SCL; preferred signal."""
    processing_baseline: str | None = None

    @property
    def ranking_fraction(self) -> float | None:
        """AOI fraction if known, else the tile percentage scaled to 0-1, else None."""
        if self.aoi_cloud_fraction is not None:
            return self.aoi_cloud_fraction
        if self.tile_cloud_cover is not None:
            return self.tile_cloud_cover / 100.0
        return None


def collapse_reprocessings(candidates: Sequence[SceneCandidate]) -> list[SceneCandidate]:
    """Keep one candidate per acquisition instant, preferring the newest processing baseline.

    Earth Search lists reprocessed scenes twice (`..._0_L2A` baseline 02.14 and `..._1_L2A`
    baseline 05.00) with the same `datetime` to the millisecond.
    """
    best: dict[int, SceneCandidate] = {}
    for c in candidates:
        key = round(c.acquired.timestamp())
        current = best.get(key)
        if current is None or (c.processing_baseline or "") > (current.processing_baseline or ""):
            best[key] = c
    return sorted(best.values(), key=lambda c: c.acquired)


def select_scenes(
    candidates: Sequence[SceneCandidate],
    *,
    n: int,
    max_fraction: float | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> list[SceneCandidate]:
    """Best `n` scenes by cloud fraction (ascending), ties broken by later acquisition.

    Candidates with no ranking signal at all are excluded: an unknown cloud state is not a
    clear sky. Candidates above `max_fraction` are excluded. Result is ordered by acquisition.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    pool: list[tuple[float, datetime, SceneCandidate]] = []
    for c in candidates:
        if window is not None and not (window[0] <= c.acquired <= window[1]):
            continue
        fraction = c.ranking_fraction
        if fraction is None:
            continue
        if max_fraction is not None and fraction > max_fraction:
            continue
        pool.append((fraction, c.acquired, c))
    pool.sort(key=lambda t: (t[0], -t[1].timestamp()))
    chosen = [c for _f, _t, c in pool[:n]]
    return sorted(chosen, key=lambda c: c.acquired)


def select_pre_post(
    candidates: Sequence[SceneCandidate],
    *,
    event_time: datetime,
    n_pre: int,
    n_post: int,
    max_fraction: float | None = None,
) -> tuple[list[SceneCandidate], list[SceneCandidate]]:
    """Best `n_pre` scenes strictly before `event_time` and best `n_post` at/after it."""
    pre = [c for c in candidates if c.acquired < event_time]
    post = [c for c in candidates if c.acquired >= event_time]
    return (
        select_scenes(pre, n=n_pre, max_fraction=max_fraction),
        select_scenes(post, n=n_post, max_fraction=max_fraction),
    )
