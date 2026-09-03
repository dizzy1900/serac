"""Golden record of the detector stub's ratio sequence on a real fixture.

The golden file pins what the placeholder computes on `data/fixtures/seismic/<event>/` so an
accidental change to the buffer, window or band edges is caught. It pins *values*, not a
verdict: whether any ratio crosses the placeholder threshold is recorded as an observation
(`fired`), never asserted. Regenerate with `serac stream golden --update` (or
`SERAC_UPDATE_GOLDEN=1` around the golden test) after an intentional change, and say why in
the commit message.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from serac.streaming.detector_stub import (
    DETECTOR_NAME,
    DETECTOR_VERSION,
    DetectorStub,
    DetectorStubConfig,
)
from serac.streaming.replay_source import FixtureReplaySource, fixture_dir_for

GOLDEN_CONTRACT_VERSION = "0.1.0"
GOLDEN_SUBDIR = Path("tests") / "fixtures" / "golden"
DEFAULT_EVENT = "chamoli-2021"
DEFAULT_CHUNK_SECONDS = 5.0
RATIO_DIGITS = 6


def golden_path(repo_root: Path, event_id: str = DEFAULT_EVENT) -> Path:
    return repo_root / GOLDEN_SUBDIR / f"detector_stub_{event_id}.json"


def _round(value: float) -> float | str:
    if value != value or value in (float("inf"), float("-inf")):
        return repr(value)
    return float(f"{value:.{RATIO_DIGITS}g}")


def compute_golden(
    repo_root: Path,
    event_id: str = DEFAULT_EVENT,
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    config: DetectorStubConfig | None = None,
) -> dict[str, Any]:
    """Run the stub over the fixture and return the JSON-ready golden document."""
    source = FixtureReplaySource(fixture_dir_for(repo_root, event_id), repo_root=repo_root)
    detector = DetectorStub(config or DetectorStubConfig())
    for chunk in source.chunks(chunk_seconds=chunk_seconds):
        detector.evaluate(chunk)
    samples = [
        {
            "sncl": h.sncl,
            "window_end_utc": h.window_end_utc.isoformat(),
            "n_samples": h.n_samples,
            "ratio": _round(h.ratio),
            "fired": h.fired,
        }
        for h in detector.history
    ]
    return {
        "contract_version": GOLDEN_CONTRACT_VERSION,
        "event_id": event_id,
        "detector": DETECTOR_NAME,
        "detector_version": DETECTOR_VERSION,
        "chunk_seconds": chunk_seconds,
        "params": detector.config.as_params(),
        "fixtures": [ref.model_dump() for ref in source.fixture_refs()],
        "n_chunks": detector.chunks_seen,
        "n_ratios": len(samples),
        "n_fired": sum(1 for s in samples if s["fired"]),
        "note": (
            "Ratios of the STUB detector on a real fixture, pinned to 6 significant digits. "
            "`fired` is an observation at the placeholder threshold, not a target."
        ),
        "samples": samples,
    }


def write_golden(document: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_golden(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def diff_golden(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable differences between two golden documents (empty when equal)."""
    problems: list[str] = []
    for key in ("event_id", "detector", "detector_version", "chunk_seconds", "params", "fixtures"):
        if expected.get(key) != actual.get(key):
            problems.append(f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}")
    exp_samples = expected.get("samples", [])
    act_samples = actual.get("samples", [])
    if len(exp_samples) != len(act_samples):
        problems.append(f"sample count: expected {len(exp_samples)}, got {len(act_samples)}")
    for index, (e, a) in enumerate(zip(exp_samples, act_samples, strict=False)):
        for problem in _sample_diff(index, e, a):
            problems.append(problem)
        if len(problems) > 10:
            problems.append("... further differences omitted")
            break
    return problems


# The ratio is an FFT-derived float. Different BLAS/FFT builds (macOS arm64 vs Linux x86_64)
# agree only to within rounding, so the golden pins the value to a relative tolerance rather
# than bit-for-bit. Anything that actually changes the algorithm moves the ratio far more
# than this, and every non-float field is still compared exactly.
RATIO_REL_TOL = 1e-6


def _sample_diff(index: int, expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    keys = set(expected) | set(actual)
    for key in sorted(keys):
        e, a = expected.get(key), actual.get(key)
        if key == "ratio" and isinstance(e, int | float) and isinstance(a, int | float):
            if not math.isclose(float(e), float(a), rel_tol=RATIO_REL_TOL, abs_tol=1e-12):
                problems.append(f"sample {index}.ratio: expected {e}, got {a}")
        elif e != a:
            problems.append(f"sample {index}.{key}: expected {e!r}, got {a!r}")
    return problems
