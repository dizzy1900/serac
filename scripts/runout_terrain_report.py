"""Regenerate `reports/runout/terrain.json`: the corridor terrain and thalweg statistics.

Committed so that every figure quoted from it has a generator in the tree. The thalweg
statistic that the runout write-ups lean on is `thalweg_fraction_below_mu_threshold`: the
fraction of binned thalweg segments whose slope cannot sustain motion under a Voellmy Coulomb
coefficient of `THALWEG_THRESHOLD_MU`. Both the coefficient and its slope angle are written out,
so nothing downstream has to convert or remember them.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from serac.models.runout.corridor import load_frame, roundtrip_rms_px
from serac.models.runout.params import NOT_RAVAFLOW
from serac.models.runout.terrain import (
    THALWEG_THRESHOLD_MU,
    corridor_terrain,
    thalweg_is_draining,
)

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
AOI = "lhende-khola-trishuli"
RESOLUTIONS = (90.0, 60.0, 30.0)
BINS = 500


def main() -> None:
    frame = load_frame(REPO / "data" / "aoi" / AOI, 32645)
    threshold_deg = math.degrees(math.atan(THALWEG_THRESHOLD_MU))
    out: dict[str, object] = {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "generator": "scripts/runout_terrain_report.py",
        "disclaimer": NOT_RAVAFLOW,
        "thalweg_threshold_mu": THALWEG_THRESHOLD_MU,
        "thalweg_threshold_deg": round(threshold_deg, 3),
        "thalweg_bins": BINS,
        "centreline": {
            "length_m": frame.length_m,
            "samples": frame.n_samples,
            "spacing_m": frame.spacing_m,
            "note": "OSM river geometry from the committed AOI, ODbL.",
        },
        "note": (
            "The round trip is exact where the closest-point projection is interior and "
            "undefined where it snaps to a vertex -- the corridor buffer's end-caps and outer "
            "bends, beyond the centreline's medial axis, where the map is genuinely "
            "many-to-one. Both sets are reported. `thalweg_fraction_below_mu_threshold` is the "
            "share of binned thalweg segments whose slope cannot sustain motion at a Voellmy "
            f"Coulomb coefficient of {THALWEG_THRESHOLD_MU:g}, i.e. below "
            f"{threshold_deg:.3f} degrees."
        ),
    }
    rows = []
    for res in RESOLUTIONS:
        terrain = corridor_terrain(REPO, aoi_id=AOI, resolution_m=res)
        summary = terrain.summary()
        draining, worst = thalweg_is_draining(terrain)
        grid = terrain.grid
        xs = grid.x_min + res * (np.arange(grid.width) + 0.5)
        ys = grid.y_max - res * (np.arange(grid.height) + 0.5)
        xx, yy = np.meshgrid(xs, ys)
        rms_valid, max_valid = roundtrip_rms_px(
            frame, xx[terrain.frame_valid], yy[terrain.frame_valid], res
        )
        outside = terrain.domain_mask & ~terrain.frame_valid
        rms_out, max_out = roundtrip_rms_px(frame, xx[outside], yy[outside], res)
        centres, profile = terrain.thalweg_profile(BINS)
        ok = np.isfinite(profile)
        slope = -np.diff(profile[ok]) / np.diff(centres[ok])
        summary.update(
            {
                "thalweg_draining": draining,
                "thalweg_worst_rise_m": worst,
                "roundtrip_rms_px_frame_valid": rms_valid,
                "roundtrip_max_px_frame_valid": max_valid,
                "roundtrip_rms_px_outside_frame_valid": rms_out,
                "roundtrip_max_px_outside_frame_valid": max_out,
                "thalweg_segments": int(slope.size),
                "thalweg_slope_deg_p10": float(np.degrees(np.arctan(np.percentile(slope, 10)))),
                "thalweg_slope_deg_p50": float(np.degrees(np.arctan(np.percentile(slope, 50)))),
                "thalweg_slope_deg_p90": float(np.degrees(np.arctan(np.percentile(slope, 90)))),
                "thalweg_fraction_below_mu_threshold": float((slope < THALWEG_THRESHOLD_MU).mean()),
                "thalweg_fraction_below_1_deg": float((slope < math.tan(math.radians(1.0))).mean()),
            }
        )
        rows.append(summary)
    out["resolutions"] = rows
    path = REPO / "reports" / "runout" / "terrain.json"
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")  # noqa: T201
    for row in rows:
        print(  # noqa: T201
            f"  {row['resolution_m']:.0f} m: "
            f"below mu={THALWEG_THRESHOLD_MU:g} ({threshold_deg:.2f} deg) "
            f"{row['thalweg_fraction_below_mu_threshold']:.4f}, "
            f"median slope {row['thalweg_slope_deg_p50']:.3f} deg, "
            f"round trip {row['roundtrip_rms_px_frame_valid']:.2e} px"
        )


if __name__ == "__main__":
    main()
