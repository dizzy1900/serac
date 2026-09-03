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
        if e != a:
            problems.append(f"sample {index}: expected {e}, got {a}")
            if len(problems) > 10:
                problems.append("... further differences omitted")
                break
    return problems
