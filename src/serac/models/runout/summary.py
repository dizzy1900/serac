"""What the ensemble actually contains, counted rather than asserted.

Written to `reports/runout/ensemble_summary.json` and read by `validate-runout`. The counts
that matter are the number of **valid** members (the brief's floor is 200), the flag reasons on
the retained-but-flagged ones, the distribution of how far the flow got, and the total bytes on
disk against the 3 GB cap.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from serac.models.runout.driver import INDEX_FILENAME, iter_index
from serac.models.runout.ensemble import design_from_payload, read_frozen_design
from serac.models.runout.params import SOLVER_NAME, SOLVER_VERSION

SUMMARY_FILENAME = "ensemble_summary.json"
BYTES_CAP = 3 * 1024**3


def _normalise_reason(reason: str) -> str:
    """Strip the per-member counts so flags group into kinds rather than one bucket each."""
    without_counts = re.sub(r"\d[\d.eE+-]*", "N", reason)
    return without_counts.split("(")[0].strip()


def directory_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def summarise_ensemble(repo: Path, reports_dir: Path, *, write: bool = True) -> dict[str, Any]:
    """Count members, flags, runout and bytes; write `ensemble_summary.json`."""
    index_path = reports_dir / INDEX_FILENAME
    rows = list(iter_index(index_path)) if index_path.exists() else []
    latest: dict[int, dict[str, Any]] = {int(r["index"]): r for r in rows}
    members = list(latest.values())

    valid = [m for m in members if m.get("valid")]
    flagged = [m for m in members if m.get("flag_reasons")]
    reasons: Counter[str] = Counter()
    for member in flagged:
        for reason in member["flag_reasons"]:
            reasons[_normalise_reason(reason)] += 1

    reach = np.array(
        [float(m["results"]["reach_m"]) for m in valid if "results" in m], dtype=np.float64
    )
    wall = np.array([float(m.get("wall_time_s", 0.0)) for m in members], dtype=np.float64)
    mu = np.array(
        [float(m["parameters"]["mu"]) for m in valid if "parameters" in m], dtype=np.float64
    )

    root = repo / "data" / "interim" / "runout"
    total_bytes = directory_bytes(root) if root.exists() else 0

    design_payload = None
    design_hash = None
    try:
        design_payload = read_frozen_design(reports_dir)
        design_hash = design_from_payload(design_payload).design_hash
    except FileNotFoundError:
        pass

    def percentiles(
        values: np.ndarray, qs: tuple[int, ...] = (5, 25, 50, 75, 95)
    ) -> dict[str, float]:
        if values.size == 0:
            return {}
        return {f"p{q}": round(float(np.percentile(values, q)), 2) for q in qs}

    summary: dict[str, Any] = {
        "solver": {"name": SOLVER_NAME, "version": SOLVER_VERSION},
        "design_hash": design_hash,
        "frozen_solver_version": (design_payload or {}).get("solver_version"),
        "n_members_recorded": len(members),
        "n_valid": len(valid),
        "n_invalid": len(members) - len(valid),
        "n_flagged_but_retained": len([m for m in flagged if m.get("valid")]),
        "flag_reasons": dict(reasons.most_common()),
        "reach_km": {
            **{k: round(v / 1000.0, 3) for k, v in percentiles(reach).items()},
            "max": round(float(reach.max()) / 1000.0, 3) if reach.size else None,
            "min": round(float(reach.min()) / 1000.0, 3) if reach.size else None,
        },
        "wall_time_s": percentiles(wall),
        "wall_time_total_core_s": round(float(wall.sum()), 1),
        "mu": percentiles(mu),
        "bytes_on_disk": total_bytes,
        "bytes_cap": BYTES_CAP,
        "bytes_within_cap": total_bytes <= BYTES_CAP,
        "resolution_counts": dict(
            Counter(str(m.get("resolution_m")) for m in members).most_common()
        ),
    }
    if write:
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / SUMMARY_FILENAME).write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    return summary
