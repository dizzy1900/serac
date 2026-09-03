"""Sentinel-2 feature tracking by orientation correlation. **This is not autoRIFT.**

Say it plainly, because the brief asked for autoRIFT and this is not it: autoRIFT is not on
PyPI, and vendoring it from JPL/ASF drags in ISCE2, which does not build on this machine in the
time available. So M3 uses a documented orientation-correlation NCC tracker written here.

The differences that matter to anyone reading a number out of this module:

* autoRIFT searches over a **variable, iteratively refined** chip size and uses a sparse-then-
  dense pyramid; this tracker uses one fixed chip size and one pass. It will therefore be
  noisier on small, fast features and blind to displacements larger than its search radius.
* autoRIFT is the engine behind ITS_LIVE. **Numbers from this module are not comparable with
  ITS_LIVE velocities** and must never be tabulated alongside them as if they were.
* No post-filtering beyond the correlation-peak quality threshold and the noise floor below.

Why orientation correlation rather than plain NCC on brightness: the complex orientation image
``O = (Gx + i Gy) / |Gx + i Gy|`` throws away gradient magnitude and keeps only direction, so a
pair separated by months — with different sun elevation, different snow cover and different
atmospheric haze — still correlates on terrain structure rather than on brightness. It is the
standard trick for repeat optical imagery (Fitch et al. 2002) and it is what makes a summer /
autumn pair usable at all.

**The noise floor is measured, not assumed.** For every pair, the displacement field is
evaluated over stable ground — slope below 10 degrees, not glacier, not water — and the median
absolute displacement there is that pair's noise floor. A per-unit displacement below its
pair's floor is reported as not significant. A tracker without a measured floor produces
plausible motion everywhere, which is precisely the failure mode this component must not have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
from numpy.typing import NDArray
from scipy import fft as spfft

FloatArray = NDArray[np.float64]

TRACKER_ID: Final[str] = "serac-orientation-correlation-v1"
NOT_AUTORIFT: Final[str] = (
    "NOT autoRIFT and NOT comparable with ITS_LIVE: autoRIFT is unavailable on PyPI and "
    "vendoring it requires ISCE2. This is a single-pass, fixed-chip orientation-correlation "
    "NCC tracker written for serac."
)

DEFAULT_CHIP_PX: Final[int] = 32
DEFAULT_STEP_PX: Final[int] = 16
DEFAULT_MAX_SHIFT_PX: Final[int] = 8
MIN_PEAK_QUALITY: Final[float] = 0.10
STABLE_MAX_SLOPE_DEG: Final[float] = 10.0


def orientation_image(band: FloatArray) -> NDArray[np.complex128]:
    """Unit-magnitude complex gradient direction; zero where the gradient vanishes."""
    gy, gx = np.gradient(np.asarray(band, dtype=np.float64))
    gradient = gx + 1j * gy
    magnitude = np.abs(gradient)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(magnitude > 0, gradient / magnitude, 0.0 + 0.0j)
    return np.asarray(np.nan_to_num(out), dtype=np.complex128)


def _parabolic_subpixel(values: FloatArray, peak: int) -> float:
    """Sub-sample peak offset from the three samples around `peak`; 0.0 at an array edge."""
    if peak <= 0 or peak >= values.size - 1:
        return 0.0
    left, centre, right = values[peak - 1], values[peak], values[peak + 1]
    denominator = left - 2.0 * centre + right
    if denominator == 0:
        return 0.0
    return float(0.5 * (left - right) / denominator)


@dataclass(frozen=True)
class ChipMatch:
    """One chip's displacement, in pixels, with the correlation-peak quality behind it."""

    row: int
    col: int
    dx_px: float
    dy_px: float
    quality: float


def match_chip(
    reference: FloatArray, secondary: FloatArray, *, max_shift_px: int = DEFAULT_MAX_SHIFT_PX
) -> tuple[float, float, float]:
    """`(dx, dy, quality)` in pixels for one chip pair, by FFT orientation correlation.

    `dx` is positive eastward (increasing column), `dy` positive southward (increasing row),
    matching the raster's own axes. Quality is the correlation peak normalised by the mean
    correlation magnitude, so it is scale-free and comparable between chips.
    """
    if reference.shape != secondary.shape:
        raise ValueError("chips must have the same shape")
    if min(reference.shape) < 4:
        raise ValueError("chips must be at least 4x4")
    o1 = orientation_image(reference)
    o2 = orientation_image(secondary)
    if not np.any(o1) or not np.any(o2):
        return 0.0, 0.0, 0.0
    # `fft2(secondary) * conj(fft2(reference))`, in that order. For a feature that moved by
    # +d between the two scenes, F2 = F1 * exp(-i k d), so F2 * conj(F1) has phase -k d and its
    # inverse transform peaks at +d. The other order peaks at -d and silently reverses every
    # displacement in the report.
    spectrum = spfft.fft2(o2) * np.conj(spfft.fft2(o1))
    correlation = np.abs(spfft.fftshift(spfft.ifft2(spectrum)))
    centre = (correlation.shape[0] // 2, correlation.shape[1] // 2)
    lo_r, hi_r = centre[0] - max_shift_px, centre[0] + max_shift_px + 1
    lo_c, hi_c = centre[1] - max_shift_px, centre[1] + max_shift_px + 1
    window = correlation[max(lo_r, 0) : hi_r, max(lo_c, 0) : hi_c]
    if window.size == 0:
        return 0.0, 0.0, 0.0
    flat = int(np.argmax(window))
    wr, wc = divmod(flat, window.shape[1])
    peak_r = wr + max(lo_r, 0)
    peak_c = wc + max(lo_c, 0)
    mean = float(correlation.mean())
    quality = 0.0 if mean <= 0 else float((correlation[peak_r, peak_c] - mean) / mean)
    # A peak sitting on the edge of the search window is not a measurement: either the true
    # displacement is larger than the search radius, or the chip decorrelated and the "peak" is
    # the largest value of a flat noise field, which piles up at the boundary. Either way the
    # value is unusable, so it is failed rather than reported. Without this, a decorrelated
    # scene produces a field of displacements all equal to the search radius, which looks like
    # coherent motion.
    on_edge = wr in (0, window.shape[0] - 1) or wc in (0, window.shape[1] - 1)
    if on_edge:
        return 0.0, 0.0, 0.0
    dr = _parabolic_subpixel(correlation[:, peak_c], peak_r)
    dc = _parabolic_subpixel(correlation[peak_r, :], peak_c)
    return float(peak_c + dc - centre[1]), float(peak_r + dr - centre[0]), quality


def track_pair(
    reference: FloatArray,
    secondary: FloatArray,
    *,
    chip_px: int = DEFAULT_CHIP_PX,
    step_px: int = DEFAULT_STEP_PX,
    max_shift_px: int = DEFAULT_MAX_SHIFT_PX,
) -> list[ChipMatch]:
    """Track a whole scene pair on a regular chip grid."""
    if reference.shape != secondary.shape:
        raise ValueError("scenes must share a shape")
    rows, cols = reference.shape
    out: list[ChipMatch] = []
    for r in range(0, rows - chip_px + 1, step_px):
        for c in range(0, cols - chip_px + 1, step_px):
            ref = reference[r : r + chip_px, c : c + chip_px]
            sec = secondary[r : r + chip_px, c : c + chip_px]
            if not (np.isfinite(ref).all() and np.isfinite(sec).all()):
                continue
            dx, dy, quality = match_chip(ref, sec, max_shift_px=max_shift_px)
            out.append(
                ChipMatch(
                    row=r + chip_px // 2, col=c + chip_px // 2, dx_px=dx, dy_px=dy, quality=quality
                )
            )
    return out


@dataclass(frozen=True)
class NoiseFloor:
    """A pair's measured stable-ground noise floor, and the sample it came from."""

    median_abs_displacement_m: float
    p95_abs_displacement_m: float
    n_stable_chips: int
    definition: str = (
        "median |displacement| over chips whose centre has slope < 10 degrees, is not inside an "
        "RGI 7.0 glacier outline, and is not water"
    )

    HEAVY_TAIL_RATIO: float = 5.0
    """`p95 / median` above which the median stops being a usable discriminator."""

    def is_significant(self, displacement_m: float) -> bool:
        return abs(displacement_m) > self.median_abs_displacement_m

    @property
    def degenerate(self) -> bool:
        """Is the median floor exactly zero, making the significance test vacuous?

        On stable ground the correlation peak lands on the zero-shift sample and the sub-pixel
        fit returns exactly 0, so more than half the stable chips can be exactly zero and the
        median floor collapses to 0.0 — at which point "displacement exceeds the floor" is true
        of every non-zero measurement. Observed on the Langtang scenes. The pre-registered rule
        is left alone and this flag is reported instead; the p95 in the same record is the
        statistic a reader should use.
        """
        return not (
            np.isfinite(self.median_abs_displacement_m) and self.median_abs_displacement_m > 0.0
        )

    @property
    def heavy_tailed(self) -> bool:
        """Is the stable-ground distribution so skewed that the median understates the noise?

        Pre-registration section 9 fixes the median as the floor, and that threshold is not
        changed here. But when the 95th percentile of the *same stable-ground sample* is many
        times the median, the median is describing the well-behaved chips and saying nothing
        about the tail, so a "significant" flag derived from it means very little. Reporting
        the ratio is the honest response; moving the threshold would be tuning.
        """
        if not (
            np.isfinite(self.median_abs_displacement_m)
            and np.isfinite(self.p95_abs_displacement_m)
            and self.median_abs_displacement_m > 0
        ):
            return True
        return bool(
            self.p95_abs_displacement_m / self.median_abs_displacement_m > self.HEAVY_TAIL_RATIO
        )


def measure_noise_floor(
    matches: list[ChipMatch], stable: NDArray[np.bool_], pixel_m: float
) -> NoiseFloor:
    """The floor for one pair. Raises nothing: an empty stable sample gives a NaN floor."""
    values = [
        float(np.hypot(m.dx_px, m.dy_px) * pixel_m)
        for m in matches
        if 0 <= m.row < stable.shape[0] and 0 <= m.col < stable.shape[1] and stable[m.row, m.col]
    ]
    if not values:
        return NoiseFloor(float("nan"), float("nan"), 0)
    array = np.asarray(values, dtype=np.float64)
    return NoiseFloor(
        median_abs_displacement_m=float(np.median(array)),
        p95_abs_displacement_m=float(np.percentile(array, 95)),
        n_stable_chips=int(array.size),
    )


def aggregate_to_units(
    matches: list[ChipMatch],
    labels: NDArray[np.int32],
    unit_ids: dict[int, str],
    pixel_m: float,
    floor: NoiseFloor,
    *,
    min_quality: float = MIN_PEAK_QUALITY,
    min_chips: int = 3,
) -> dict[str, dict[str, Any]]:
    """Median displacement magnitude per slope unit, flagged against the pair's noise floor."""
    buckets: dict[int, list[float]] = {}
    for m in matches:
        if m.quality < min_quality:
            continue
        if not (0 <= m.row < labels.shape[0] and 0 <= m.col < labels.shape[1]):
            continue
        label = int(labels[m.row, m.col])
        if label <= 0:
            continue
        buckets.setdefault(label, []).append(float(np.hypot(m.dx_px, m.dy_px) * pixel_m))
    out: dict[str, dict[str, Any]] = {}
    for label, unit_id in unit_ids.items():
        values = buckets.get(label, [])
        if len(values) < min_chips:
            out[unit_id] = {
                "displacement_m": None,
                "n_chips": len(values),
                "significant": False,
                "reason": "too few good chips inside the unit",
            }
            continue
        median = float(np.median(values))
        out[unit_id] = {
            "displacement_m": round(median, 3),
            "n_chips": len(values),
            "significant": bool(floor.is_significant(median)),
            "reason": None
            if floor.is_significant(median)
            else "below the pair's measured stable-ground noise floor",
        }
    return out


def run_optical_tracking(
    *,
    data_dir: Path,
    reports_dir: Path,
    aoi_dir: Path,
    aoi_id: str,
    window_start: datetime,
    window_end: datetime,
    max_pairs: int = 12,
    online: bool = False,
    chip_px: int = DEFAULT_CHIP_PX,
    step_px: int = DEFAULT_STEP_PX,
) -> dict[str, Any]:
    """Track every consecutive usable Sentinel-2 pair in the window and write the report."""
    from serac.models.watch.optical_io import (
        SceneStack,
        load_scene_stack,
        stable_ground_mask,
    )

    stack: SceneStack = load_scene_stack(
        data_dir=data_dir,
        aoi_dir=aoi_dir,
        aoi_id=aoi_id,
        window_start=window_start,
        window_end=window_end,
        online=online,
    )
    summary: dict[str, Any] = {
        "aoi_id": aoi_id,
        "tracker_id": TRACKER_ID,
        "not_autorift": NOT_AUTORIFT,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "window": [window_start.date().isoformat(), window_end.date().isoformat()],
        "chip_px": chip_px,
        "step_px": step_px,
        "pixel_m": stack.pixel_m,
        "n_scenes": len(stack.scenes),
        "scenes": [s.as_dict() for s in stack.scenes],
        "pairs": [],
        "status": "ok",
    }
    if len(stack.scenes) < 2:
        summary["status"] = "insufficient_scenes"
        summary["note"] = (
            f"{len(stack.scenes)} usable Sentinel-2 scene(s) in the window; feature tracking "
            "needs at least two. This is an observability result, not a deformation result."
        )
        return _write(reports_dir, aoi_id, summary)

    stable = stable_ground_mask(stack, data_dir=data_dir, aoi_dir=aoi_dir, aoi_id=aoi_id)
    summary["n_stable_pixels"] = int(stable.sum())
    for reference, secondary in list(zip(stack.scenes, stack.scenes[1:], strict=False))[:max_pairs]:
        matches = track_pair(reference.band, secondary.band, chip_px=chip_px, step_px=step_px)
        floor = measure_noise_floor(matches, stable, stack.pixel_m)
        units = aggregate_to_units(matches, stack.labels, stack.unit_ids, stack.pixel_m, floor)
        significant = [u for u, v in units.items() if v["significant"]]
        summary["pairs"].append(
            {
                "reference": reference.product_id,
                "secondary": secondary.product_id,
                "reference_date": reference.acquired_at.date().isoformat(),
                "secondary_date": secondary.acquired_at.date().isoformat(),
                "days": round(
                    (secondary.acquired_at - reference.acquired_at).total_seconds() / 86_400.0, 1
                ),
                "n_chips": len(matches),
                "noise_floor_m": None
                if not np.isfinite(floor.median_abs_displacement_m)
                else round(floor.median_abs_displacement_m, 3),
                "noise_floor_p95_m": None
                if not np.isfinite(floor.p95_abs_displacement_m)
                else round(floor.p95_abs_displacement_m, 3),
                "noise_floor_definition": floor.definition,
                "n_stable_chips": floor.n_stable_chips,
                "noise_floor_heavy_tailed": floor.heavy_tailed,
                "noise_floor_degenerate": floor.degenerate,
                "noise_floor_caveat": _floor_caveat(floor),
                "n_units_measured": sum(
                    1 for v in units.values() if v["displacement_m"] is not None
                ),
                "n_units_significant": len(significant),
                "top_units": sorted(
                    (
                        {"unit_id": u, **v}
                        for u, v in units.items()
                        if v["displacement_m"] is not None
                    ),
                    key=lambda r: -float(r["displacement_m"]),
                )[:10],
            }
        )
    return _write(reports_dir, aoi_id, summary)


def _floor_caveat(floor: NoiseFloor) -> str | None:
    """How badly the pre-registered median floor misdescribes this pair, if at all."""
    if floor.degenerate:
        return (
            "the stable-ground median is 0.0 m, so the pre-registered significance test "
            "(displacement > median) is satisfied by every non-zero measurement and means "
            "nothing for this pair; use the p95 in this record instead. The threshold is left "
            "as pre-registered rather than moved to fit this observation."
        )
    if floor.heavy_tailed:
        return (
            "the stable-ground distribution is heavy-tailed (p95 / median > "
            f"{NoiseFloor.HEAVY_TAIL_RATIO:.0f}), so the pre-registered median floor describes "
            "the well-behaved chips only and the significance flag derived from it should not "
            "be read as a detection"
        )
    return None


def _write(reports_dir: Path, aoi_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    out = reports_dir / "watch" / f"optical_{aoi_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    payload["report_path"] = out.as_posix()
    return payload
