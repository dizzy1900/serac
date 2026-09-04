"""Turn a track selection plus a burst listing into a concrete, costed, persisted network plan.

The plan is a file, not a computation repeated at submit time, so that what was submitted is
exactly what was costed and reviewed. `submit-insar` and `poll-insar` both read it back.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

from serac.adapters.eo.asf_bursts import read_listing
from serac.domain.geo import GridSpec
from serac.errors import SeracError
from serac.models.watch.crop import WatchGrid, watch_grid
from serac.models.watch.network import (
    LOOKS_PIXEL_M,
    MULTI_BURST_ONE_CREDIT_MAX,
    NetworkBudget,
    NetworkPair,
    acquisitions_from_bursts,
    budget,
    plan_pairs,
)
from serac.models.watch.raster import load_grid_spec
from serac.models.watch.track_select import BurstScene, bursts_from_features

BURST_PRESENCE_FRACTION = 0.90
"""A burst must appear in this fraction of the track's passes to be part of the stable set.

Below it, requiring the burst would throw away most of the archive; the alternative — letting
the burst set vary per pair — would make the product extent depend on delivery order.
"""

PROCESSED_POLARIZATION = "VV"
"""Co-polarised VV only.

Dual-polarisation Sentinel-1 acquisitions deliver each burst twice, and HyP3 refuses a job
whose granules mix polarisations. VV is the co-polarised channel with the higher backscatter
and the better coherence over rock and ice, and it is the channel acquired on every pass here,
whereas VH is not.
"""

RETAINED_RASTERS_PER_PAIR = 2
"""`_unw_phase` and `_corr` per pair; the geometry rasters are kept once for the whole stack."""


class NetworkPlan(BaseModel):
    """The persisted network: the pairs, the grid they will be cropped to, and the budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aoi_id: str
    path_number: int
    flight_direction: str
    looks: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    burst_ids: list[str] = Field(min_length=1)
    burst_listing_path: str
    burst_listing_sha256: str
    n_conn: int
    max_bt_days: float
    annual_anchors: bool = True
    crop_grid: dict[str, Any]
    aoi_coverage_fraction: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of the AOI bbox inside the union of the processed burst footprints",
    )
    source_zone_covered: bool | None = Field(
        default=None,
        description="Whether the AOI's source zone lies inside the processed footprints; null "
        "when the AOI has no source_zone.geojson",
    )
    coverage_notes: list[str] = Field(default_factory=list)
    pairs: list[NetworkPair]
    budget: NetworkBudget
    plan_sha256: str = ""

    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    @property
    def watch_grid(self) -> WatchGrid:
        return WatchGrid(**self.crop_grid)


def network_plan_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "interim" / "watch" / f"network_{aoi_id}.json"


def load_network_plan(data_dir: Path, aoi_id: str) -> NetworkPlan:
    path = network_plan_path(data_dir, aoi_id)
    if not path.exists():
        raise SeracError(f"no network plan at {path}; run `serac watch plan-network` first")
    return NetworkPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_burst_ids(
    bursts: list[BurstScene], *, aoi_bbox: tuple[float, float, float, float], max_bursts: int
) -> list[str]:
    """The burst set to process: present in nearly every pass, ranked by AOI overlap.

    Ranking by the area each burst shares with the AOI, rather than by burst number, means a
    burst that only clips a corner is the one dropped when the count has to be capped to stay
    inside HyP3's one-credit tier.
    """
    passes: set[str] = {b.acquisition.strftime("%Y-%m-%dT%H") for b in bursts}
    counts: dict[str, set[str]] = {}
    overlap: dict[str, float] = {}
    aoi = box(*aoi_bbox)
    for b in bursts:
        key = b.full_burst_id
        counts.setdefault(key, set()).add(b.acquisition.strftime("%Y-%m-%dT%H"))
        if key not in overlap:
            try:
                overlap[key] = float(Polygon(b.footprint).intersection(aoi).area)
            except Exception:
                overlap[key] = 0.0
    threshold = BURST_PRESENCE_FRACTION * max(len(passes), 1)
    stable = [k for k, v in counts.items() if len(v) >= threshold and overlap.get(k, 0.0) > 0.0]
    stable.sort(key=lambda k: (-overlap[k], k))
    return sorted(stable[:max_bursts])


def build_network_plan(
    *,
    data_dir: Path,
    reports_dir: Path,
    aoi_id: str,
    window_start: datetime,
    window_end: datetime,
    path_number: int | None,
    n_conn: int,
    max_bt_days: float,
    looks: str,
) -> NetworkPlan:
    """Assemble the plan from the committed track selection and the cached burst listing."""
    selection_path = reports_dir / "watch" / f"track_selection_{aoi_id}.json"
    if not selection_path.exists():
        raise SeracError(
            f"no track selection at {selection_path}; run `serac watch select-track` first"
        )
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    chosen = path_number if path_number is not None else selection.get("selected_path")
    if chosen is None:
        raise SeracError(
            "the frozen rule selected no track; pass --path explicitly and say so in the report"
        )
    listing_path = Path(selection["burst_listing"])
    bursts = [
        b
        for b in bursts_from_features(read_listing(listing_path))
        if b.path_number == int(chosen) and b.polarization.upper() == PROCESSED_POLARIZATION
    ]
    if not bursts:
        raise SeracError(f"the burst listing holds no granules for path {chosen}")

    raw_bbox = json.loads((data_dir / "aoi" / aoi_id / "aoi.json").read_text())[
        "cube_extent_bbox_4326"
    ]
    aoi_bbox: tuple[float, float, float, float] = (
        float(raw_bbox[0]),
        float(raw_bbox[1]),
        float(raw_bbox[2]),
        float(raw_bbox[3]),
    )
    max_bursts = MULTI_BURST_ONE_CREDIT_MAX[looks]
    burst_ids = stable_burst_ids(bursts, aoi_bbox=aoi_bbox, max_bursts=max_bursts)
    if not burst_ids:
        raise SeracError("no burst is present in enough passes to form a stable stack")

    wanted = {_relative(bid) for bid in burst_ids}
    acquisitions = acquisitions_from_bursts(
        ((b.acquisition, b.scene_name) for b in bursts if _relative(b.full_burst_id) in wanted),
        required_burst_ids=sorted(wanted),
    )
    pairs = plan_pairs(acquisitions, n_conn=n_conn, max_bt_days=max_bt_days, annual_anchors=True)

    coverage, source_covered, coverage_notes = _footprint_coverage(
        bursts, burst_ids, aoi_bbox=aoi_bbox, aoi_dir=data_dir / "aoi" / aoi_id
    )

    grid: GridSpec = load_grid_spec(data_dir / "aoi" / aoi_id)
    crop_grid = watch_grid(grid, LOOKS_PIXEL_M[looks])
    measured = _measured_product_bytes(data_dir, aoi_id)
    plan = NetworkPlan(
        aoi_id=aoi_id,
        path_number=int(chosen),
        flight_direction=bursts[0].flight_direction,
        looks=looks,
        window_start=window_start,
        window_end=window_end,
        burst_ids=burst_ids,
        burst_listing_path=listing_path.as_posix(),
        burst_listing_sha256=_sha256(listing_path),
        n_conn=n_conn,
        max_bt_days=max_bt_days,
        crop_grid=crop_grid.as_dict(),
        aoi_coverage_fraction=coverage,
        source_zone_covered=source_covered,
        coverage_notes=coverage_notes,
        pairs=pairs,
        budget=budget(
            pairs,
            aoi_id=aoi_id,
            path_number=int(chosen),
            looks=looks,
            n_acquisitions=len(acquisitions),
            n_bursts=len(burst_ids),
            crop_pixels=crop_grid.pixels,
            retained_rasters=RETAINED_RASTERS_PER_PAIR,
            measured_product_bytes=measured,
        ),
    )
    return plan.model_copy(update={"plan_sha256": plan.digest()})


def _relative(full_burst_id: str) -> str:
    """`056_118313_IW1` -> `118313_IW1`, matching the granule naming."""
    parts = full_burst_id.split("_")
    return "_".join(parts[1:]) if len(parts) == 3 else full_burst_id


def _measured_product_bytes(data_dir: Path, aoi_id: str) -> int | None:
    """Median delivered zip size from the jobs ledger, or None before anything has landed."""
    from serac.models.watch.insar_jobs import jobs_ledger_path, read_jobs

    path = jobs_ledger_path(data_dir, aoi_id)
    if not path.exists():
        return None
    sizes = [j.zip_bytes for j in read_jobs(path) if j.zip_bytes]
    if not sizes:
        return None
    sizes.sort()
    return sizes[len(sizes) // 2]


def _footprint_coverage(
    bursts: list[BurstScene],
    burst_ids: list[str],
    *,
    aoi_bbox: tuple[float, float, float, float],
    aoi_dir: Path,
) -> tuple[float, bool | None, list[str]]:
    """How much of the AOI the processed burst set actually images.

    A Sentinel-1 subswath is roughly 85 km wide, so a long downstream corridor AOI is mostly
    outside any single track's footprint. Recording the covered fraction — and separately
    whether the source zone is inside it — keeps that from turning into a silent truncation:
    slope units outside the footprint get no InSAR at all and must be reported as
    insufficient-data rather than quiet.
    """
    wanted = set(burst_ids)
    shapes = {b.full_burst_id: Polygon(b.footprint) for b in bursts if b.full_burst_id in wanted}
    if not shapes:
        return 0.0, None, ["no processed burst footprint could be reconstructed"]
    union = unary_union(list(shapes.values()))
    aoi = box(*aoi_bbox)
    coverage = float(union.intersection(aoi).area / aoi.area) if aoi.area else 0.0
    notes: list[str] = []
    if coverage < 0.9:
        notes.append(
            f"the processed burst set images {coverage:.1%} of the AOI bounding box; the rest is "
            "outside this track's subswath and has no InSAR coverage at all"
        )
    source_covered: bool | None = None
    source_path = aoi_dir / "source_zone.geojson"
    if source_path.exists():
        features = json.loads(source_path.read_text(encoding="utf-8")).get("features") or []
        if features:
            zone = unary_union([shape(f["geometry"]) for f in features])
            source_covered = bool(union.contains(zone))
            notes.append(
                "source zone "
                + ("is" if source_covered else "is NOT")
                + " inside the processed footprints"
            )
    return coverage, source_covered, notes
