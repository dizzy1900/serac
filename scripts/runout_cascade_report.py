"""Cascade rules v0 across the ensemble: damming index, breach hydrograph, secondary surge.

Writes `reports/runout/cascade_v0.json` and `reports/runout/CASCADE_V0.md`. Every number
carries its assumption string, and the 30 m resolution limitation is stated **where the damming
numbers are**, not only in the model card.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from serac.models.runout.cascade import (
    DAMMING_V0_LABEL,
    breach_hydrograph,
    damming_index,
    find_constrictions,
    index_to_probability,
    secondary_surge,
)
from serac.models.runout.corridor import load_frame, transect_chainages
from serac.models.runout.driver import INDEX_FILENAME, iter_index
from serac.models.runout.params import NOT_RAVAFLOW, RESOLUTION_LIMITATION

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
REPORTS = REPO / "reports" / "runout"
AOI = "lhende-khola-trishuli"


def main() -> None:
    rows = list(iter_index(REPORTS / INDEX_FILENAME))
    valid = [r for r in rows if r.get("valid")]
    if not valid:
        raise SystemExit("no valid members")

    deposits: list[np.ndarray] = []
    chainage: np.ndarray | None = None
    bed: np.ndarray | None = None
    for row in valid:
        parquet = REPO / row["directory"] / "corridor.parquet"
        if not parquet.exists():
            continue
        table = pq.read_table(parquet)
        if chainage is None:
            chainage = np.asarray(table["chainage_m"], dtype=np.float64)
            bed = np.asarray(table["bed_min_m"], dtype=np.float64)
        deposits.append(np.asarray(table["deposit_depth_m"], dtype=np.float64))
    assert chainage is not None and bed is not None
    stack = np.stack(deposits)
    median = np.median(stack, axis=0)
    p95 = np.percentile(stack, 95, axis=0)

    frame = load_frame(REPO / "data" / "aoi" / AOI, 32645)
    transects = transect_chainages(REPO / "data" / "aoi" / AOI, frame)

    sites: list[dict[str, Any]] = []
    for site in find_constrictions(chainage, bed, n_sites=12):
        idx = int(np.argmin(np.abs(chainage - site.chainage_m)))
        deposit_median = float(median[idx])
        deposit_p95 = float(p95[idx])
        if deposit_p95 <= 0.0:
            continue
        indicator = damming_index(site, deposit_median if deposit_median > 0 else deposit_p95)
        upper = damming_index(site, deposit_p95)
        hydrograph = breach_hydrograph(upper)
        surges = []
        for transect in transects:
            surge = secondary_surge(upper, hydrograph, transect.frame_chainage_m)
            if surge is not None:
                surges.append({"transect_id": transect.transect_id, **surge.as_dict()})
        sites.append(
            {
                **indicator.as_dict(),
                "deposit_depth_median_m": round(deposit_median, 3),
                "deposit_depth_p95_m": round(deposit_p95, 3),
                "damming_index_at_p95_deposit": round(upper.index, 4),
                "probability_uncalibrated_median": round(index_to_probability(indicator.index), 4),
                "probability_uncalibrated_p95": round(index_to_probability(upper.index), 4),
                "breach_at_p95_deposit": hydrograph.as_dict(),
                "secondary_surges": surges,
            }
        )

    payload = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "label": DAMMING_V0_LABEL,
        "members_used": len(deposits),
        "note": (
            "Deposit depths are the ensemble median and 95th percentile per chainage bin, not a "
            "single member. The index is evaluated at the median and the breach and surge at the "
            "95th percentile, so the surge numbers are an upper case rather than a central one."
        ),
        "disclaimer": NOT_RAVAFLOW,
        "resolution_limitation": RESOLUTION_LIMITATION,
        "sites": sites,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "cascade_v0.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    site_rows = "\n".join(
        f"| {s['chainage_m'] / 1000:.1f} | {s['channel_depth_m']:.1f} | "
        f"{s['deposit_depth_median_m']:.1f} | {s['deposit_depth_p95_m']:.1f} | "
        f"{s['damming_index']:.2f} | {s['probability_uncalibrated_median']:.2f} | "
        f"{s['breach_at_p95_deposit']['peak_discharge_m3s']:.0f} |"
        for s in sites
    )
    surge_rows = ""
    for s in sites[:3]:
        for surge in s["secondary_surges"]:
            surge_rows += (
                f"| {s['chainage_m'] / 1000:.1f} | `{surge['transect_id']}` | "
                f"{surge['travel_time_s'] / 60:.1f} | {surge['celerity_m_s']:.1f} | "
                f"{surge['peak_discharge_m3s']:.0f} |\n"
            )

    text = f"""# Cascade rules v0 — damming, breach, secondary surge

> {NOT_RAVAFLOW}

**Read this before using any number below.** {RESOLUTION_LIMITATION}

These are a dimensionless index and a set of parametric relations, not an engineering estimate
of a landslide dam. In particular:

* the damming index is deposit depth over channel depth, **both measured on the same 30 m DEM**,
  so both sides of the ratio carry the same resolution error;
* the "probability" column is that index put through a stated logistic with a midpoint at index
  1 and a scale of 0.4. It is **not** estimated from data. No inventory of landslide dams exists
  for this corridor, and one event is not a sample;
* the breach hydrograph is a triangular wave whose area is the impounded volume and whose peak
  follows a published-form regression. It is not routed, has no sediment and no progressive
  erosion;
* the secondary-surge arrival translates that peak downstream at a constant celerity. It is not
  a solved flood routing, and it ignores attenuation, so it **over-states** the surge downstream.

Deposit depths are the ensemble median and 95th percentile per chainage bin over
{len(deposits)} valid members. The index is evaluated at the median deposit; the breach and
surge at the 95th percentile, so those are an upper case rather than a central one.

## Candidate damming sites

| Chainage (km) | Channel (m) | Deposit p50 | Deposit p95 | Index | p(dam) | Breach peak (m3/s) |
|---|---|---|---|---|---|---|
{site_rows or "| (none found) | — | — | — | — | — | — |"}

## Secondary-surge arrivals from the three most upstream sites

Minutes after the breach begins, not after the detachment.

| Dam at (km) | Transect | Travel (min) | Celerity (m/s) | Peak (m3/s) |
|---|---|---|---|---|
{surge_rows or "| (none) | — | — | — | — |"}
"""
    (REPORTS / "CASCADE_V0.md").write_text(text, encoding="utf-8")
    print(f"wrote {REPORTS / 'cascade_v0.json'} and CASCADE_V0.md")  # noqa: T201


if __name__ == "__main__":
    main()
