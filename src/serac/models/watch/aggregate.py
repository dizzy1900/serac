"""Collapse the pixel-level MintPy time series onto slope units: `watch_cube.zarr`.

Dimensions are `(unit, time)`. Every variable carries its own provenance attributes, so a
consumer reading one variable can tell where it came from without reading the report.

Two aggregation choices worth stating:

* **Median, not mean.** A slope unit at 80 m pixels holds a few dozen to a few hundred pixels,
  and an unwrapping error in one of them is a whole 2-pi cycle. The median absorbs that; the
  mean does not.
* **Coherence-weighted membership.** A pixel joins its unit's series only if its own temporal
  coherence clears `MIN_PIXEL_TEMPORAL_COHERENCE`. A unit with too few surviving pixels gets
  NaN for that epoch rather than a median over three noisy pixels, which is why
  `n_pixels_valid` is stored alongside every value: "no measurement" and "a measurement of
  zero" are different and stay different.

`coherence_loss` is the fraction of a unit's pixels whose interferometric coherence for that
epoch's nearest pair fell below the anomaly model's `MIN_COHERENCE`. It is a *data-quality*
variable, not a deformation variable, and it is what tells the Langtang write-up whether a
quiet unit was actually observed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
from numpy.typing import NDArray

from serac.errors import SeracError
from serac.models.watch.anomaly import MIN_COHERENCE

TIMESERIES_PREFERENCE: Final[tuple[str, ...]] = (
    "timeseries_tropHgt_ramp_demErr.h5",
    "timeseries_tropHgt_ramp.h5",
    "timeseries_tropHgt.h5",
    "timeseries.h5",
)
"""Most-corrected time series first.

MintPy writes one file per correction stage and leaves the raw `timeseries.h5` in place, so
reading `timeseries.h5` silently discards the tropospheric correction, the ramp removal and
the DEM-error correction — every correction the pipeline was configured to apply. The file
actually used is recorded in the cube attributes and in the report.
"""

CORRECTIONS_APPLIED: Final[dict[str, str]] = {
    "timeseries.h5": "none (network inversion only)",
    "timeseries_tropHgt.h5": "height-correlation tropospheric delay",
    "timeseries_tropHgt_ramp.h5": "height-correlation troposphere, linear ramp",
    "timeseries_tropHgt_ramp_demErr.h5": ("height-correlation troposphere, linear ramp, DEM error"),
}

MIN_PIXEL_TEMPORAL_COHERENCE: Final[float] = 0.40
MIN_PIXELS_PER_UNIT: Final[int] = 5
EPOCH: Final[datetime] = datetime(2014, 1, 1, tzinfo=UTC)
RADIANS_TO_MM_C_BAND: Final[float] = -55.465763 / (4.0 * np.pi)
"""Sentinel-1 C-band: wavelength 55.465763 mm, and MintPy hands back metres already, so this
constant exists only for the raw-radian path and is not used on a MintPy timeseries."""

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class UnitCube:
    """The aggregated arrays plus the metadata that goes into the zarr attributes."""

    unit_ids: list[str]
    times: list[datetime]
    los_mm: FloatArray
    coherence: FloatArray
    n_pixels_valid: NDArray[np.int32]
    coherence_loss: FloatArray
    los_sensitivity_signed: FloatArray
    inside_footprint: NDArray[np.bool_]
    n_pixels_total: NDArray[np.int32]
    attrs: dict[str, Any]


def watch_cube_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "features" / "watch" / aoi_id / "watch_cube.zarr"


def select_timeseries(work_dir: Path) -> Path:
    """The most-corrected MintPy time series present, per `TIMESERIES_PREFERENCE`."""
    for name in TIMESERIES_PREFERENCE:
        candidate = work_dir / name
        if candidate.exists():
            return candidate
    raise SeracError(
        f"no MintPy time series under {work_dir}; run `serac watch mintpy` first "
        f"(looked for {', '.join(TIMESERIES_PREFERENCE)})"
    )


def _read_h5(path: Path, dataset: str) -> Any:
    import h5py  # type: ignore[import-not-found,import-untyped,unused-ignore]

    with h5py.File(path, "r") as fh:
        if dataset not in fh:
            return None
        return np.asarray(fh[dataset][()])


def _read_h5_attrs(path: Path) -> dict[str, Any]:
    import h5py  # type: ignore[import-not-found,import-untyped,unused-ignore]

    with h5py.File(path, "r") as fh:
        return {k: (v.decode() if isinstance(v, bytes) else v) for k, v in fh.attrs.items()}


def _dates(path: Path) -> list[datetime]:
    raw = _read_h5(path, "date")
    if raw is None:
        raise SeracError(f"{path} has no `date` dataset")
    out: list[datetime] = []
    for value in raw:
        text = value.decode() if isinstance(value, bytes) else str(value)
        out.append(datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC))
    return out


def build_unit_cube(
    *,
    timeseries_h5: Path,
    temporal_coherence_h5: Path,
    labels: NDArray[np.int32],
    unit_ids: dict[int, str],
    los_sensitivity_signed: dict[str, float],
    average_spatial_coherence: FloatArray | None = None,
) -> UnitCube:
    """Median LOS displacement per unit per epoch, plus coherence and validity counts."""
    series = _read_h5(timeseries_h5, "timeseries")
    if series is None:
        raise SeracError(f"{timeseries_h5} has no `timeseries` dataset")
    times = _dates(timeseries_h5)
    temporal_coherence = _read_h5(temporal_coherence_h5, "temporalCoherence")
    if temporal_coherence is None:
        raise SeracError(f"{temporal_coherence_h5} has no `temporalCoherence` dataset")

    series_mm = np.asarray(series, dtype=np.float64) * 1000.0  # MintPy timeseries is in metres
    n_time = series_mm.shape[0]
    if series_mm.shape[1:] != labels.shape:
        raise SeracError(
            f"timeseries grid {series_mm.shape[1:]} does not match the slope-unit label grid "
            f"{labels.shape}; the crop grid and the AOI grid have diverged"
        )

    ordered = [unit_ids[k] for k in sorted(unit_ids)]
    index = {unit_ids[k]: i for i, k in enumerate(sorted(unit_ids))}
    n_units = len(ordered)
    los = np.full((n_units, n_time), np.nan, dtype=np.float64)
    coh = np.full((n_units, n_time), np.nan, dtype=np.float64)
    valid = np.zeros((n_units, n_time), dtype=np.int32)
    loss = np.full((n_units, n_time), np.nan, dtype=np.float64)
    totals = np.zeros(n_units, dtype=np.int32)
    inside = np.zeros(n_units, dtype=np.bool_)

    good_pixel = np.isfinite(temporal_coherence) & (
        temporal_coherence >= MIN_PIXEL_TEMPORAL_COHERENCE
    )
    spatial = (
        average_spatial_coherence
        if average_spatial_coherence is not None
        else np.asarray(temporal_coherence, dtype=np.float64)
    )
    for label in sorted(unit_ids):
        i = index[unit_ids[label]]
        mask = labels == label
        totals[i] = int(mask.sum())
        if totals[i] == 0:
            continue
        usable = mask & good_pixel
        inside[i] = bool(np.isfinite(temporal_coherence[mask]).any())
        n_usable = int(usable.sum())
        unit_spatial = spatial[mask]
        unit_spatial = unit_spatial[np.isfinite(unit_spatial)]
        loss_fraction = (
            float((unit_spatial < MIN_COHERENCE).mean()) if unit_spatial.size else np.nan
        )
        if n_usable < MIN_PIXELS_PER_UNIT:
            loss[i, :] = loss_fraction
            continue
        window = series_mm[:, usable]
        finite = np.isfinite(window)
        counts = finite.sum(axis=1).astype(np.int32)
        with np.errstate(invalid="ignore"):
            medians = np.nanmedian(np.where(finite, window, np.nan), axis=1)
        los[i, :] = np.where(counts >= MIN_PIXELS_PER_UNIT, medians, np.nan)
        valid[i, :] = counts
        coh[i, :] = float(np.median(temporal_coherence[usable]))
        loss[i, :] = loss_fraction

    sensitivity = np.array(
        [los_sensitivity_signed.get(u, np.nan) for u in ordered], dtype=np.float64
    )
    return UnitCube(
        unit_ids=ordered,
        times=times,
        los_mm=los,
        coherence=coh,
        n_pixels_valid=valid,
        coherence_loss=loss,
        los_sensitivity_signed=sensitivity,
        inside_footprint=inside,
        n_pixels_total=totals,
        attrs=_read_h5_attrs(timeseries_h5),
    )


def write_watch_cube(cube: UnitCube, path: Path, *, provenance: dict[str, Any]) -> Path:
    """Write `watch_cube.zarr` with per-variable provenance attributes."""
    import xarray as xr

    common = {
        "source": provenance.get("source", "MintPy smallbaselineApp on HyP3 burst InSAR"),
        "aoi_id": provenance.get("aoi_id"),
        "path_number": provenance.get("path_number"),
        "mintpy_config_sha256": provenance.get("mintpy_config_sha256"),
        "delineation_sha256": provenance.get("delineation_sha256"),
        "network_plan_sha256": provenance.get("network_plan_sha256"),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    dataset = xr.Dataset(
        {
            "los_displacement": (
                ("unit", "time"),
                cube.los_mm,
                {
                    **common,
                    "long_name": "median line-of-sight displacement per slope unit",
                    "units": "mm",
                    "positive": "towards the satellite",
                    "processing": (
                        "median over unit pixels with temporal coherence >= "
                        f"{MIN_PIXEL_TEMPORAL_COHERENCE}; NaN where fewer than "
                        f"{MIN_PIXELS_PER_UNIT} pixels are valid"
                    ),
                    "tropospheric_correction": provenance.get("tropospheric_correction"),
                    "timeseries_file": provenance.get("timeseries_file"),
                    "corrections_applied": provenance.get("corrections_applied"),
                },
            ),
            "temporal_coherence": (
                ("unit", "time"),
                cube.coherence,
                {
                    **common,
                    "long_name": "median MintPy temporal coherence of the unit's usable pixels",
                    "units": "1",
                    "processing": "constant in time; MintPy reports one value per pixel",
                },
            ),
            "n_pixels_valid": (
                ("unit", "time"),
                cube.n_pixels_valid,
                {
                    **common,
                    "long_name": "pixels contributing to the median at this epoch",
                    "units": "count",
                    "processing": "zero distinguishes 'not measured' from 'measured as zero'",
                },
            ),
            "coherence_loss": (
                ("unit", "time"),
                cube.coherence_loss,
                {
                    **common,
                    "long_name": (
                        "fraction of the unit's pixels below the anomaly model's coherence floor"
                    ),
                    "units": "1",
                    "processing": f"fraction of pixels with coherence < {MIN_COHERENCE}",
                    "note": "a data-quality variable, not a deformation variable",
                },
            ),
            "los_sensitivity_signed": (
                ("unit",),
                cube.los_sensitivity_signed,
                {
                    **common,
                    "long_name": "signed projection of downslope motion onto the line of sight",
                    "units": "1",
                    "processing": "downslope . LOS from the GLO-30 DEM and the track geometry",
                },
            ),
            "inside_footprint": (
                ("unit",),
                cube.inside_footprint,
                {**common, "long_name": "unit has any InSAR coverage at all", "units": "bool"},
            ),
            "n_pixels_total": (
                ("unit",),
                cube.n_pixels_total,
                {**common, "long_name": "pixels in the unit", "units": "count"},
            ),
        },
        coords={
            "unit": np.array(cube.unit_ids, dtype=object),
            "time": np.array([t.replace(tzinfo=None) for t in cube.times], dtype="datetime64[s]"),
        },
        attrs={
            **common,
            "title": f"serac M3 slope-watch cube ({provenance.get('aoi_id')})",
            "tier_disclaimer": (
                "The watch tier derived from this cube is ordinal. It is not a calibrated "
                "failure probability and it is never a prediction of a failure date."
            ),
            "mintpy_attrs": json.dumps({k: str(v) for k, v in cube.attrs.items()}, sort_keys=True)[
                :20000
            ],
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        import shutil

        shutil.rmtree(path)
    dataset.to_zarr(path, mode="w", zarr_format=3, consolidated=False)
    return path


def days_since_epoch(when: datetime) -> float:
    return (when - EPOCH).total_seconds() / 86_400.0


def epoch_plus(days: float) -> datetime:
    return EPOCH + timedelta(days=days)


def build_watch_cube(*, data_dir: Path, reports_dir: Path, aoi_id: str) -> dict[str, Any]:
    """Read MintPy's output and the slope-unit labels, aggregate, and write the zarr."""
    import geopandas as gpd
    import rasterio

    from serac.models.watch.mintpy_run import mintpy_dir
    from serac.models.watch.plan import load_network_plan
    from serac.models.watch.slope_units import labels_path, slope_units_path

    plan = load_network_plan(data_dir, aoi_id)
    work = mintpy_dir(data_dir, aoi_id, "pass2")
    timeseries = select_timeseries(work)
    mintpy_report = reports_dir / "watch" / f"mintpy_{aoi_id}.json"
    mintpy_meta = (
        json.loads(mintpy_report.read_text(encoding="utf-8")) if mintpy_report.exists() else {}
    )

    # Slope-unit labels are on the 30 m AOI grid; the InSAR stack is on the 80 m crop grid.
    # Resample the labels by nearest neighbour so a unit keeps its identity exactly.
    with rasterio.open(labels_path(data_dir, aoi_id)) as src:
        labels_30m = src.read(1).astype(np.int32)
        src_transform, src_crs = src.transform, src.crs
    labels = _resample_labels(labels_30m, src_transform, src_crs, plan.watch_grid)

    frame = gpd.read_parquet(slope_units_path(data_dir, aoi_id))
    unit_ids = {int(r.unit_index): str(r.unit_id) for r in frame.itertuples()}
    unit_ids = {k: v for k, v in unit_ids.items() if (labels == k).any()}

    heading = float(mintpy_meta.get("heading_deg") or 0.0)
    incidence = float(mintpy_meta.get("mean_incidence_deg") or 0.0)
    sensitivity = _unit_sensitivity(frame, unit_ids, incidence=incidence, heading=heading)

    cube = build_unit_cube(
        timeseries_h5=timeseries,
        temporal_coherence_h5=work / "temporalCoherence.h5",
        labels=labels,
        unit_ids=unit_ids,
        los_sensitivity_signed=sensitivity,
        average_spatial_coherence=_avg_spatial_coherence(work),
    )
    out = write_watch_cube(
        cube,
        watch_cube_path(data_dir, aoi_id),
        provenance={
            "aoi_id": aoi_id,
            "path_number": plan.path_number,
            "mintpy_config_sha256": mintpy_meta.get("config_sha256"),
            "delineation_sha256": str(frame["delineation_sha256"].iloc[0]),
            "network_plan_sha256": plan.plan_sha256,
            "tropospheric_correction": mintpy_meta.get("tropospheric_correction"),
            "timeseries_file": timeseries.name,
            "corrections_applied": CORRECTIONS_APPLIED.get(timeseries.name, "unknown"),
        },
    )
    summary = {
        "aoi_id": aoi_id,
        "cube_path": out.as_posix(),
        "n_units": len(cube.unit_ids),
        "n_epochs": len(cube.times),
        "first_epoch": cube.times[0].isoformat() if cube.times else None,
        "last_epoch": cube.times[-1].isoformat() if cube.times else None,
        "units_with_any_measurement": int(np.isfinite(cube.los_mm).any(axis=1).sum()),
        "units_inside_footprint": int(cube.inside_footprint.sum()),
        "median_coherence_loss": float(np.nanmedian(cube.coherence_loss))
        if np.isfinite(cube.coherence_loss).any()
        else None,
        "tropospheric_correction": mintpy_meta.get("tropospheric_correction"),
        "timeseries_file": timeseries.name,
        "corrections_applied": CORRECTIONS_APPLIED.get(timeseries.name, "unknown"),
        "mintpy_reference_point": mintpy_meta.get("reference_point"),
    }
    report = reports_dir / "watch" / f"watch_cube_{aoi_id}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def _resample_labels(
    labels: NDArray[np.int32], transform: Any, crs: Any, grid: Any
) -> NDArray[np.int32]:
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    destination = np.zeros((grid.height, grid.width), dtype=np.int32)
    reproject(
        source=labels,
        destination=destination,
        src_transform=transform,
        src_crs=crs,
        dst_transform=grid.transform,
        dst_crs=CRS.from_epsg(grid.epsg),
        src_nodata=0,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    return destination


def _avg_spatial_coherence(work: Path) -> FloatArray | None:
    path = work / "avgSpatialCoh.h5"
    if not path.exists():
        return None
    data = _read_h5(path, "coherence")
    return None if data is None else np.asarray(data, dtype=np.float64)


def _unit_sensitivity(
    frame: Any, unit_ids: dict[int, str], *, incidence: float, heading: float
) -> dict[str, float]:
    """Signed `downslope . LOS` per unit, from the unit's own mean slope and aspect."""
    from serac.models.watch.geometry import downslope_unit_vector, los_unit_vector

    out: dict[str, float] = {}
    wanted = set(unit_ids.values())
    for row in frame.itertuples():
        if str(row.unit_id) not in wanted:
            continue
        de, dn, du = downslope_unit_vector(float(row.mean_slope_deg), float(row.aspect_deg))
        le, ln, lu = los_unit_vector(incidence, heading)
        out[str(row.unit_id)] = float(de * le + dn * ln + du * lu)
    return out


__all__ = [
    "MIN_PIXELS_PER_UNIT",
    "MIN_PIXEL_TEMPORAL_COHERENCE",
    "UnitCube",
    "build_unit_cube",
    "build_watch_cube",
    "days_since_epoch",
    "epoch_plus",
    "watch_cube_path",
    "write_watch_cube",
]


ELEVATION_BANDS: Final[tuple[tuple[float, float], ...]] = (
    (0.0, 3000.0),
    (3000.0, 4000.0),
    (4000.0, 4500.0),
    (4500.0, 5000.0),
    (5000.0, 5500.0),
    (5500.0, 9000.0),
)


def coherence_by_elevation(
    data_dir: Path, aoi_id: str, *, threshold: float = MIN_PIXEL_TEMPORAL_COHERENCE
) -> list[dict[str, Any]]:
    """Median MintPy temporal coherence per elevation band, and the fraction above `threshold`.

    This is the measurement behind the C-band decorrelation limitation. Stating it as a table
    rather than as a sentence is what turns "C-band decorrelates over snow and ice" from a
    caveat into a number a reader can check.
    """
    import rasterio

    from serac.models.watch.mintpy_run import mintpy_dir

    work = mintpy_dir(data_dir, aoi_id, "pass2")
    coherence = _read_h5(work / "temporalCoherence.h5", "temporalCoherence")
    if coherence is None:
        return []
    dem_tif = next(
        iter((data_dir / "raw" / "hyp3_burst_insar" / aoi_id).glob("S1_*/*_dem.tif")), None
    )
    if dem_tif is None:
        return []
    with rasterio.open(dem_tif) as src:
        elevation = src.read(1).astype(np.float64)
    if elevation.shape != coherence.shape:
        return []
    usable = np.isfinite(coherence) & np.isfinite(elevation) & (elevation > 0)
    out: list[dict[str, Any]] = []
    for low, high in ELEVATION_BANDS:
        band = usable & (elevation >= low) & (elevation < high)
        n = int(band.sum())
        if n < 50:
            continue
        out.append(
            {
                "elevation_m": [low, high],
                "n_pixels": n,
                "median_temporal_coherence": round(float(np.median(coherence[band])), 4),
                "fraction_above_threshold": round(float((coherence[band] >= threshold).mean()), 4),
            }
        )
    total = usable.sum()
    if total:
        out.append(
            {
                "elevation_m": None,
                "n_pixels": int(total),
                "median_temporal_coherence": round(float(np.median(coherence[usable])), 4),
                "fraction_above_threshold": round(
                    float((coherence[usable] >= threshold).mean()), 4
                ),
            }
        )
    return out
