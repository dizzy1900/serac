"""Choose one Sentinel-1 relative orbit per AOI, by a rule fixed before the numbers exist.

Four relative orbits cross the Chamoli AOI and the archive can only be processed once, so the
choice of track is a real scientific decision that could be made after the fact to flatter a
result. It is therefore made the other way round: `SELECTION_RULE` below is the whole rule,
it is committed before `serac watch select-track` is ever run, and the command applies it
mechanically and reports every input it used.

What the rule weighs
--------------------
1. **LOS sensitivity** — the mean of ``|d . u|`` over the AOI's steep terrain, i.e. what
   fraction of downslope motion the track can see at all. A track that cannot see the motion
   cannot flag it however good its coherence.
2. **Layover and shadow** — the fraction of that same steep terrain the track geometrically
   cannot image. This is the reason north faces are so often invisible.
3. **Sampling** — the scene count over the window and the largest gap between consecutive
   acquisitions. A track with a six-month hole cannot support a monthly walk-forward.

The mask is deliberately the same for every track (`STEEP_SLOPE_MIN_DEG` over the whole AOI)
and is **not** the failure's source zone. Restricting the mask to where a landslide is known
to have happened would tune the acquisition geometry to the answer.

Eligibility is a hard filter and the score only orders what survives it, so a track cannot buy
its way past a data gap with good geometry.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, Final

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.models.watch.geometry import (
    INCIDENCE_BASIS,
    IW_NOMINAL_INCIDENCE_DEG,
    heading_from_footprint,
    layover_shadow_masks,
    los_sensitivity,
    slope_aspect,
)
from serac.models.watch.raster import GriddedDem

STEEP_SLOPE_MIN_DEG: Final[float] = 25.0
"""Terrain shallower than this is not a rock-slope hazard and only dilutes the statistics."""

MIN_SCENES: Final[int] = 100
MAX_GAP_DAYS: Final[float] = 90.0
MAX_LAYOVER_SHADOW_FRACTION: Final[float] = 0.35
NOMINAL_REVISIT_DAYS: Final[float] = 12.0

SELECTION_RULE: Final[str] = """\
Frozen track-selection rule (serac M3, committed before the first run)

Mask: all AOI pixels with slope >= 25 degrees on the AOI's own 30 m grid, identical for every
candidate track. The mask is not conditioned on any known failure location.

Per candidate relative orbit, over the requested window:
  sensitivity        = mean over the mask of |downslope . line-of-sight|
  layover_shadow     = fraction of the mask flagged layover or shadow by the local-slope test
  n_scenes           = distinct acquisition dates found by the ASF burst search
  max_gap_days       = largest interval between consecutive acquisition dates
  coverage           = min(1, n_scenes / (window_days / 12))

Eligible if and only if:
  n_scenes >= 100  AND  max_gap_days <= 90  AND  layover_shadow <= 0.35

Score (eligible tracks only):
  score = sensitivity * (1 - layover_shadow) * coverage

Choose the eligible track with the highest score. Ties break on lower max_gap_days, then on
lower path number. If no track is eligible the command selects nothing, reports the reason per
track, and the operator decides; it never relaxes a threshold on its own.
"""


class TrackMetrics(BaseModel):
    """Everything the rule looked at for one relative orbit, so the choice can be re-derived."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path_number: int
    flight_direction: str
    subswaths: list[str]
    heading_deg: float
    incidence_deg: float
    incidence_basis: str = INCIDENCE_BASIS
    n_scenes: int
    n_bursts: int
    first_acquisition: AwareDatetime | None = None
    last_acquisition: AwareDatetime | None = None
    max_gap_days: float
    median_gap_days: float | None = None
    los_sensitivity: float = Field(ge=0.0, le=1.0)
    layover_fraction: float = Field(ge=0.0, le=1.0)
    shadow_fraction: float = Field(ge=0.0, le=1.0)
    layover_shadow_fraction: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0)
    eligible: bool
    ineligibility_reasons: list[str] = Field(default_factory=list)


class TrackSelection(BaseModel):
    """The output of `serac watch select-track`: the rule, every candidate, and the choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aoi_id: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    slope_mask_min_deg: float = STEEP_SLOPE_MIN_DEG
    slope_mask_pixels: int
    dem_source_path: str
    dem_source_sha256: str
    selection_rule: str = SELECTION_RULE
    rule_sha256: str
    candidates: list[TrackMetrics]
    selected_path: int | None
    selected_reason: str


def rule_digest() -> str:
    """sha256 of `SELECTION_RULE`, recorded so a later edit to the rule is visible."""
    import hashlib

    return hashlib.sha256(SELECTION_RULE.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BurstScene:
    """One SLC burst from the ASF search, reduced to what track selection needs."""

    path_number: int
    flight_direction: str
    subswath: str
    full_burst_id: str
    acquisition: datetime
    footprint: list[tuple[float, float]]
    polarization: str = ""
    size_bytes: int | None = None
    scene_name: str = ""


def bursts_from_features(features: Sequence[dict[str, Any]]) -> list[BurstScene]:
    """Turn ASF GeoJSON burst features into `BurstScene`s, skipping anything incomplete."""
    out: list[BurstScene] = []
    for feature in features:
        props = feature.get("properties") or {}
        burst = props.get("burst") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        path = props.get("pathNumber")
        start = props.get("startTime")
        if path is None or not start or not coords:
            continue
        ring = coords[0] if geometry.get("type") == "Polygon" else coords[0][0]
        size = props.get("bytes")
        out.append(
            BurstScene(
                path_number=int(path),
                flight_direction=str(props.get("flightDirection", "")),
                subswath=str(burst.get("subswath", "")),
                full_burst_id=str(burst.get("fullBurstID", "")),
                acquisition=datetime.fromisoformat(str(start).replace("Z", "+00:00")),
                footprint=[(float(c[0]), float(c[1])) for c in ring],
                polarization=str(props.get("polarization", "")),
                size_bytes=int(size) if size not in (None, "") else None,
                scene_name=str(props.get("sceneName", "")),
            )
        )
    return out


def steep_mask(
    dem: GriddedDem, min_slope_deg: float = STEEP_SLOPE_MIN_DEG
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(mask, slope_deg, aspect_deg)`` for the AOI's steep, finite terrain."""
    elevation = np.where(dem.valid, dem.elevation_m, np.nan)
    filled = np.where(np.isfinite(elevation), elevation, np.nanmedian(elevation))
    slope, aspect = slope_aspect(filled, dem.grid.resolution_m, dem.grid.resolution_m)
    mask = dem.valid & (slope >= min_slope_deg)
    return mask, slope, aspect


def _acquisition_dates(bursts: Sequence[BurstScene]) -> list[datetime]:
    """Distinct acquisition datetimes, one per pass (bursts of a pass share a date)."""
    seen: dict[str, datetime] = {}
    for b in bursts:
        key = b.acquisition.strftime("%Y-%m-%dT%H")
        seen.setdefault(key, b.acquisition)
    return sorted(seen.values())


def _gaps_days(dates: Sequence[datetime]) -> list[float]:
    return [(b - a).total_seconds() / 86_400.0 for a, b in pairwise(dates)]


def evaluate_track(
    bursts: Sequence[BurstScene],
    dem: GriddedDem,
    *,
    window_start: datetime,
    window_end: datetime,
    mask: np.ndarray,
    slope: np.ndarray,
    aspect: np.ndarray,
) -> TrackMetrics:
    """Apply every measurement in `SELECTION_RULE` to one relative orbit."""
    if not bursts:
        raise ValueError("evaluate_track needs at least one burst")
    path = bursts[0].path_number
    direction = bursts[0].flight_direction
    subswaths = sorted({b.subswath for b in bursts if b.subswath})
    heading = float(
        np.median([heading_from_footprint(b.footprint, b.flight_direction) for b in bursts])
    )
    incidence = float(
        np.mean(
            [IW_NOMINAL_INCIDENCE_DEG.get(s, IW_NOMINAL_INCIDENCE_DEG["IW2"]) for s in subswaths]
        )
        if subswaths
        else IW_NOMINAL_INCIDENCE_DEG["IW2"]
    )

    sensitivity_grid = los_sensitivity(slope, aspect, incidence, heading)
    layover, shadow = layover_shadow_masks(slope, aspect, incidence, heading)
    n_mask = int(mask.sum())
    sensitivity = float(sensitivity_grid[mask].mean()) if n_mask else 0.0
    layover_fraction = float(layover[mask].mean()) if n_mask else 0.0
    shadow_fraction = float(shadow[mask].mean()) if n_mask else 0.0
    combined = float((layover | shadow)[mask].mean()) if n_mask else 0.0

    dates = _acquisition_dates(bursts)
    gaps = _gaps_days(dates)
    # A gap is also open at each end of the window: an archive that starts a year late is not
    # densely sampled just because its interior is.
    edge_gaps = (
        [
            (dates[0] - window_start).total_seconds() / 86_400.0,
            (window_end - dates[-1]).total_seconds() / 86_400.0,
        ]
        if dates
        else []
    )
    max_gap = max([*gaps, *(g for g in edge_gaps if g > 0)], default=math.inf)
    window_days = (window_end - window_start).total_seconds() / 86_400.0
    expected = max(window_days / NOMINAL_REVISIT_DAYS, 1.0)
    coverage = min(1.0, len(dates) / expected)

    reasons: list[str] = []
    if len(dates) < MIN_SCENES:
        reasons.append(f"n_scenes {len(dates)} < {MIN_SCENES}")
    if max_gap > MAX_GAP_DAYS:
        reasons.append(f"max_gap_days {max_gap:.1f} > {MAX_GAP_DAYS}")
    if combined > MAX_LAYOVER_SHADOW_FRACTION:
        reasons.append(f"layover_shadow {combined:.3f} > {MAX_LAYOVER_SHADOW_FRACTION}")
    eligible = not reasons

    return TrackMetrics(
        path_number=path,
        flight_direction=direction,
        subswaths=subswaths,
        heading_deg=round(heading, 3),
        incidence_deg=round(incidence, 3),
        n_scenes=len(dates),
        n_bursts=len(bursts),
        first_acquisition=dates[0] if dates else None,
        last_acquisition=dates[-1] if dates else None,
        max_gap_days=round(max_gap, 3) if math.isfinite(max_gap) else 1e9,
        median_gap_days=round(float(np.median(gaps)), 3) if gaps else None,
        los_sensitivity=round(sensitivity, 6),
        layover_fraction=round(layover_fraction, 6),
        shadow_fraction=round(shadow_fraction, 6),
        layover_shadow_fraction=round(combined, 6),
        coverage=round(coverage, 6),
        score=round(sensitivity * (1.0 - combined) * coverage, 6) if eligible else 0.0,
        eligible=eligible,
        ineligibility_reasons=reasons,
    )


def select_track(
    bursts: Sequence[BurstScene],
    dem: GriddedDem,
    *,
    aoi_id: str,
    window_start: datetime,
    window_end: datetime,
    min_slope_deg: float = STEEP_SLOPE_MIN_DEG,
) -> TrackSelection:
    """Apply `SELECTION_RULE` to every relative orbit present in `bursts`."""
    mask, slope, aspect = steep_mask(dem, min_slope_deg)
    grouped: dict[int, list[BurstScene]] = defaultdict(list)
    for b in bursts:
        grouped[b.path_number].append(b)
    candidates = [
        evaluate_track(
            group,
            dem,
            window_start=window_start,
            window_end=window_end,
            mask=mask,
            slope=slope,
            aspect=aspect,
        )
        for _path, group in sorted(grouped.items())
    ]
    eligible = [c for c in candidates if c.eligible]
    if eligible:
        best = max(eligible, key=lambda c: (c.score, -c.max_gap_days, -c.path_number))
        selected: int | None = best.path_number
        reason = (
            f"highest score {best.score:.6f} among {len(eligible)} eligible track(s): "
            f"sensitivity {best.los_sensitivity:.3f} x (1 - layover_shadow "
            f"{best.layover_shadow_fraction:.3f}) x coverage {best.coverage:.3f}"
        )
    else:
        selected = None
        reason = (
            "no track satisfies the frozen eligibility filter; the rule selects nothing rather "
            "than relaxing a threshold. Per-track reasons are in candidates[].ineligibility_reasons"
        )
    return TrackSelection(
        aoi_id=aoi_id,
        window_start=window_start,
        window_end=window_end,
        slope_mask_min_deg=min_slope_deg,
        slope_mask_pixels=int(mask.sum()),
        dem_source_path=dem.source_path,
        dem_source_sha256=dem.source_sha256,
        rule_sha256=rule_digest(),
        candidates=candidates,
        selected_path=selected,
        selected_reason=reason,
    )
