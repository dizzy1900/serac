"""The SBAS interferogram network: which pairs to ask HyP3 for, and what they will cost.

The network is a **budget decision, not a science decision**, and is disclosed as one. A
small-baseline network over five years of 12-day Sentinel-1 has an obvious dense form —
connect everything to everything within a few months — and it is not affordable in disk,
credits or wall clock, so the shape below is a deliberate truncation:

* ``n_conn = 2`` — each acquisition is paired with the next two acquisitions. Two connections
  give the least-squares inversion a redundant path around any single decorrelated pair
  without the quadratic growth of a full network.
* ``Bt <= 36 days`` — three nominal 12-day revisits. Longer temporal baselines decorrelate
  badly over snow and ice at C-band, which is precisely the terrain of interest, so long pairs
  would mostly contribute unwrapping errors.
* **Annual anchors** — pairs spanning roughly one year, added because a chain of short pairs
  accumulates unwrapping error into a long-wavelength drift that looks exactly like slow
  creep. An anchor whose two ends fall in the same season closes that drift without asking
  the unwrapper to bridge a snow season.

What this network cannot do is measure deformation faster than about half a fringe per 12
days without unwrapping ambiguity, and it cannot see through a whole winter of decorrelation.
Both limits are in the model card.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DEFAULT_N_CONN: Final[int] = 2
DEFAULT_MAX_BT_DAYS: Final[float] = 36.0
ANNUAL_TARGET_DAYS: Final[float] = 365.25
ANNUAL_TOLERANCE_DAYS: Final[float] = 30.0
ANCHOR_MONTHS: Final[tuple[int, ...]] = (1, 7)
"""Anchors start from the first acquisition on or after 1 January and 1 July of each year."""

MULTI_BURST_CREDITS: Final[dict[str, int]] = {"20x4": 1, "10x2": 1, "5x1": 1}
"""Credits per INSAR_ISCE_MULTI_BURST job, keyed by looks, for a burst count within the
1-credit tier of the live HyP3 cost table (4 bursts at 20x4, 3 at 10x2, 1 at 5x1). The planner
refuses to guess outside that tier and asks the caller to query `/costs` instead."""

MULTI_BURST_ONE_CREDIT_MAX: Final[dict[str, int]] = {"20x4": 4, "10x2": 3, "5x1": 1}

LOOKS_PIXEL_M: Final[dict[str, float]] = {"20x4": 80.0, "10x2": 40.0, "5x1": 20.0}


@dataclass(frozen=True)
class Acquisition:
    """One pass over the AOI on the selected track: a date and the bursts it delivered."""

    acquired_at: datetime
    granules: tuple[str, ...]

    @property
    def date_str(self) -> str:
        return self.acquired_at.strftime("%Y%m%d")


class NetworkPair(BaseModel):
    """One interferogram the planner wants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    reference_date: AwareDatetime
    secondary_date: AwareDatetime
    reference_granules: list[str] = Field(min_length=1)
    secondary_granules: list[str] = Field(min_length=1)
    temporal_baseline_days: float
    kind: str = Field(description="`short` (n_conn chain) or `anchor` (annual)")


class NetworkBudget(BaseModel):
    """What `--dry-run` prints: pair count, credits, transient bytes and retained bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aoi_id: str
    path_number: int
    looks: str
    n_acquisitions: int
    n_bursts_per_acquisition: int
    n_pairs: int
    n_short_pairs: int
    n_anchor_pairs: int
    credits_per_job: int
    credits_total: int
    transient_bytes_estimate: int | None
    transient_basis: str
    retained_bytes_estimate: int
    retained_basis: str
    peak_disk_bytes_estimate: int | None
    warnings: list[str] = Field(default_factory=list)


def _anchor_starts(dates: Sequence[datetime]) -> list[datetime]:
    """First acquisition on or after 1 January / 1 July of each year the archive spans."""
    if not dates:
        return []
    out: list[datetime] = []
    for year in range(dates[0].year, dates[-1].year + 1):
        for month in ANCHOR_MONTHS:
            boundary = dates[0].replace(
                year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
            )
            later = [d for d in dates if d >= boundary]
            if later:
                out.append(later[0])
    # Distinct, in order: a sparse archive can map two boundaries onto one acquisition.
    seen: set[datetime] = set()
    distinct: list[datetime] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            distinct.append(d)
    return distinct


def plan_pairs(
    acquisitions: Sequence[Acquisition],
    *,
    n_conn: int = DEFAULT_N_CONN,
    max_bt_days: float = DEFAULT_MAX_BT_DAYS,
    annual_anchors: bool = True,
) -> list[NetworkPair]:
    """The network described in the module docstring. Deterministic in the input order."""
    if n_conn < 1:
        raise ValueError("n_conn must be >= 1")
    if max_bt_days <= 0:
        raise ValueError("max_bt_days must be > 0")
    ordered = sorted(acquisitions, key=lambda a: a.acquired_at)
    by_date = {a.acquired_at: a for a in ordered}
    dates = [a.acquired_at for a in ordered]
    chosen: dict[tuple[datetime, datetime], str] = {}

    for i, ref in enumerate(dates):
        for sec in dates[i + 1 : i + 1 + n_conn]:
            if (sec - ref).total_seconds() / 86_400.0 > max_bt_days:
                break
            chosen[(ref, sec)] = "short"

    if annual_anchors:
        for ref in _anchor_starts(dates):
            target = ref + timedelta(days=ANNUAL_TARGET_DAYS)
            window = [
                d
                for d in dates
                if abs((d - target).total_seconds() / 86_400.0) <= ANNUAL_TOLERANCE_DAYS
            ]
            if not window:
                continue
            sec = min(window, key=lambda d: (abs(d - target), d))
            chosen.setdefault((ref, sec), "anchor")

    pairs: list[NetworkPair] = []
    for (ref, sec), kind in sorted(chosen.items()):
        pairs.append(
            NetworkPair(
                pair_id=f"{ref:%Y%m%d}_{sec:%Y%m%d}",
                reference_date=ref,
                secondary_date=sec,
                reference_granules=list(by_date[ref].granules),
                secondary_granules=list(by_date[sec].granules),
                temporal_baseline_days=round((sec - ref).total_seconds() / 86_400.0, 3),
                kind=kind,
            )
        )
    return pairs


def credits_for(looks: str, n_bursts: int) -> int:
    """Credits per multi-burst job, or `ValueError` outside the tier the planner knows."""
    limit = MULTI_BURST_ONE_CREDIT_MAX.get(looks)
    if limit is None:
        raise ValueError(f"unknown looks {looks!r}")
    if n_bursts > limit:
        raise ValueError(
            f"{n_bursts} bursts at {looks} looks leaves the 1-credit tier (max {limit}); "
            "query the live HyP3 /costs endpoint and pass the cost explicitly"
        )
    return MULTI_BURST_CREDITS[looks]


def budget(
    pairs: Sequence[NetworkPair],
    *,
    aoi_id: str,
    path_number: int,
    looks: str,
    n_acquisitions: int,
    n_bursts: int,
    crop_pixels: int,
    retained_rasters: int,
    measured_product_bytes: int | None = None,
    credits_per_job: int | None = None,
) -> NetworkBudget:
    """Cost the network. `transient_bytes_estimate` is None until a product has been measured.

    Refusing to invent a per-product size before one has been delivered is the whole point:
    HyP3 publishes no size ahead of time, so the honest dry run says "unknown" and the
    operator re-runs it after the first product lands.
    """
    per_job = credits_per_job if credits_per_job is not None else credits_for(looks, n_bursts)
    retained_per_pair = crop_pixels * 4 * retained_rasters
    warnings: list[str] = []
    if measured_product_bytes is None:
        warnings.append(
            "transient bytes unknown: HyP3 publishes no product size before a job completes; "
            "re-run --dry-run after the first product to get a measured estimate"
        )
    transient = None if measured_product_bytes is None else measured_product_bytes * len(pairs)
    return NetworkBudget(
        aoi_id=aoi_id,
        path_number=path_number,
        looks=looks,
        n_acquisitions=n_acquisitions,
        n_bursts_per_acquisition=n_bursts,
        n_pairs=len(pairs),
        n_short_pairs=sum(1 for p in pairs if p.kind == "short"),
        n_anchor_pairs=sum(1 for p in pairs if p.kind == "anchor"),
        credits_per_job=per_job,
        credits_total=per_job * len(pairs),
        transient_bytes_estimate=transient,
        transient_basis=(
            "no product measured yet"
            if measured_product_bytes is None
            else (f"{measured_product_bytes:,} B measured per delivered zip x {len(pairs)} pairs")
        ),
        retained_bytes_estimate=retained_per_pair * len(pairs),
        retained_basis=(
            f"{retained_rasters} float32 rasters x {crop_pixels:,} AOI pixels at "
            f"{LOOKS_PIXEL_M.get(looks, float('nan')):.0f} m x {len(pairs)} pairs, uncompressed"
        ),
        peak_disk_bytes_estimate=(
            None
            if measured_product_bytes is None
            else measured_product_bytes * 2 + retained_per_pair * len(pairs)
        ),
        warnings=warnings,
    )


def acquisitions_from_bursts(
    bursts: Iterable[tuple[datetime, str]], *, required_burst_ids: Sequence[str] | None = None
) -> list[Acquisition]:
    """Group ``(acquired_at, granule)`` pairs into passes, keeping only complete ones.

    A pass missing one of the required bursts is dropped rather than processed short, because
    a multi-burst product with a different burst set has a different extent and MintPy would
    silently intersect the stack down to whatever all pairs happen to share.
    """
    grouped: dict[str, list[tuple[datetime, str]]] = {}
    for acquired_at, granule in bursts:
        grouped.setdefault(acquired_at.strftime("%Y-%m-%dT%H"), []).append((acquired_at, granule))
    out: list[Acquisition] = []
    for _key, rows in sorted(grouped.items()):
        rows.sort(key=lambda r: r[1])
        # Exactly one granule per burst id. A dual-polarisation acquisition delivers the same
        # burst twice (VV and VH) and HyP3 rejects a job whose granules mix polarisations, so
        # the caller filters by polarisation and this keeps the first survivor per burst.
        first_per_burst: dict[str, str] = {}
        for _t, g in rows:
            first_per_burst.setdefault(_burst_id_of(g), g)
        granules = tuple(first_per_burst[k] for k in sorted(first_per_burst))
        if required_burst_ids is not None:
            present = {_burst_id_of(g) for g in granules}
            if not set(required_burst_ids) <= present:
                continue
            granules = tuple(g for g in granules if _burst_id_of(g) in set(required_burst_ids))
        out.append(Acquisition(acquired_at=min(t for t, _g in rows), granules=granules))
    return out


def _burst_id_of(granule: str) -> str:
    """`S1_275112_IW3_20200608T124746_VV_7D8B-BURST` -> `275112_IW3` (the relative burst id)."""
    parts = granule.split("_")
    return f"{parts[1]}_{parts[2]}" if len(parts) > 2 else granule
