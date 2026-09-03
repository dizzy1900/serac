"""Convert the AOI crops into ROI_PAC binaries MintPy can read without GDAL bindings.

Why this module exists. MintPy's `processor = hyp3` path reads the delivered GeoTIFFs through
`readfile.read_gdal_vrt`, which imports `osgeo`. The GDAL Python bindings are not installed
here and cannot be: the PyPI `gdal` package needs `gdal-config` and the native headers, and
this environment has neither (rasterio ships its own libgdal but does not expose `osgeo`).

Rather than vendor a fake `osgeo`, serac converts the crops into the format MintPy reads with
plain numpy — ROI_PAC flat binaries plus `.rsc` sidecars — and drives `smallbaselineApp` with
`processor = roipac`. `mintpy/prep_roipac.py` imports no GDAL, and its `extract_metadata`
returns immediately when the `.rsc` already carries `P_BASELINE_TOP_HDR`, so the sidecars
written here make the prep step a no-op rather than something to work around.

The metadata is **not** re-derived by hand. `raster_metadata` is a rasterio reimplementation of
`readfile.read_gdal_vrt` producing the same keys, and the HyP3-specific values then come from
MintPy's own `prep_hyp3.add_hyp3_metadata`, which already understands
`INSAR_ISCE_MULTI_BURST` product names. Only two things are overridden afterwards, both
necessarily:

* `PROCESSOR` is forced back to `roipac`. `add_hyp3_metadata` sets it to `hyp3`, and MintPy
  reads that key back out of the `.rsc`, which would send it down the GDAL path again.
* `BANDS`/`INTERLEAVE`/`DATA_TYPE` describe the binary written here, not the source GeoTIFF.

ROI_PAC layout, for the record: `.unw` and `.cor` are two-band BIL float32 with amplitude in
band 1 and the quantity of interest in band 2, which is where MintPy looks for them; `.hgt` is
the same shape with height in band 2.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import rasterio
from numpy.typing import NDArray

from serac.errors import SeracError

UNW_SUFFIX: Final[str] = "_unw_phase.tif"
COR_SUFFIX: Final[str] = "_corr.tif"
DEM_SUFFIX: Final[str] = "_dem.tif"
LV_THETA_SUFFIX: Final[str] = "_lv_theta.tif"
LV_PHI_SUFFIX: Final[str] = "_lv_phi.tif"

GEOMETRY_STEM: Final[str] = "geometry"


def mintpy_inputs_dir(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "interim" / "watch" / "mintpy_inputs" / aoi_id


def raster_metadata(path: Path) -> dict[str, Any]:
    """rasterio reimplementation of `mintpy.utils.readfile.read_gdal_vrt`, same keys out.

    GDAL's geotransform gives the outer corner of the upper-left pixel, which is what ROI_PAC
    and MintPy both expect, and `rasterio`'s `transform` uses the same convention, so `c` and
    `f` carry over directly.
    """
    from mintpy.utils.readfile import standardize_metadata

    with rasterio.open(path) as src:
        transform = src.transform
        atr: dict[str, Any] = {
            "WIDTH": src.width,
            "LENGTH": src.height,
            "BANDS": src.count,
            "DATA_TYPE": str(src.dtypes[0]),
            "INTERLEAVE": "BIL",
            "X_STEP": abs(transform.a),
            "Y_STEP": -abs(transform.e),
            "X_FIRST": transform.c,
            "Y_FIRST": transform.f,
            "NoDataValue": src.nodata,
        }
        epsg = src.crs.to_epsg() if src.crs else None
        atr["EPSG"] = str(epsg) if epsg is not None else None
        if epsg is not None and 32601 <= epsg <= 32760:
            zone = epsg - (32600 if epsg < 32700 else 32700)
            atr["UTM_ZONE"] = f"{zone}{'N' if epsg < 32700 else 'S'}"
            atr["X_UNIT"] = "meters"
            atr["Y_UNIT"] = "meters"
        elif 1e-7 < abs(atr["X_STEP"]) < 1.0:
            atr["X_UNIT"] = "degrees"
            atr["Y_UNIT"] = "degrees"
    out: dict[str, Any] = dict(standardize_metadata(atr))
    return out


def _read_band(path: Path) -> NDArray[np.float32]:
    with rasterio.open(path) as src:
        data = src.read(1).astype(np.float32)
        if src.nodata is not None and np.isfinite(src.nodata):
            data = np.where(data == np.float32(src.nodata), np.float32(0.0), data)
    clean: NDArray[np.float32] = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    return clean


def write_bil_pair(amplitude: NDArray[np.float32], value: NDArray[np.float32], dest: Path) -> Path:
    """Two-band BIL float32: amplitude in band 1, the quantity in band 2."""
    if amplitude.shape != value.shape:
        raise ValueError("amplitude and value rasters must have the same shape")
    height, width = value.shape
    stacked = np.empty((height, 2, width), dtype=np.float32)
    stacked[:, 0, :] = amplitude
    stacked[:, 1, :] = value
    dest.parent.mkdir(parents=True, exist_ok=True)
    stacked.tofile(dest)
    return dest


def write_rsc(meta: dict[str, Any], dest: Path) -> Path:
    from mintpy.utils.writefile import write_roipac_rsc

    write_roipac_rsc(meta, out_file=str(dest), print_msg=False)
    return dest


def _finalise(meta: dict[str, Any], *, file_type: str, unit: str | None) -> dict[str, Any]:
    """Force the keys that describe the binary written here rather than the source GeoTIFF."""
    meta = dict(meta)
    meta["PROCESSOR"] = "roipac"
    meta["FILE_TYPE"] = file_type
    meta["BANDS"] = 2
    meta["INTERLEAVE"] = "BIL"
    meta["DATA_TYPE"] = "float32"
    meta["BYTE_ORDER"] = "little-endian"
    if unit is not None:
        meta["UNIT"] = unit
    return meta


@dataclass(frozen=True)
class PreparedPair:
    product_name: str
    unw: Path
    cor: Path


def prepare_pair(pair_dir: Path, out_dir: Path) -> PreparedPair:
    """Write `<product>.unw`, `<product>.cor` and their `.rsc` sidecars for one pair."""
    from mintpy.prep_hyp3 import add_hyp3_metadata

    unw_tif = next(iter(pair_dir.glob(f"*{UNW_SUFFIX}")), None)
    cor_tif = next(iter(pair_dir.glob(f"*{COR_SUFFIX}")), None)
    txt = next(iter(pair_dir.glob("*.txt")), None)
    if unw_tif is None or cor_tif is None or txt is None:
        raise SeracError(f"{pair_dir} is missing an unw_phase, corr or metadata file")
    product = unw_tif.name[: -len(UNW_SUFFIX)]
    out_dir.mkdir(parents=True, exist_ok=True)
    # `add_hyp3_metadata` looks for `<product>.txt` beside the file it is given.
    shutil.copy2(txt, out_dir / f"{product}.txt")

    phase = _read_band(unw_tif)
    coherence = _read_band(cor_tif)
    unw = write_bil_pair(coherence, phase, out_dir / f"{product}.unw")
    cor = write_bil_pair(coherence, coherence, out_dir / f"{product}.cor")

    base = raster_metadata(unw_tif)
    for path, file_type, unit in ((unw, ".unw", "radian"), (cor, ".cor", "1")):
        meta = add_hyp3_metadata(str(path), dict(base), is_ifg=True)
        write_rsc(
            _finalise(meta, file_type=file_type, unit=unit), path.with_suffix(path.suffix + ".rsc")
        )
    return PreparedPair(product_name=product, unw=unw, cor=cor)


GEOMETRY_SUBDIRS: Final[dict[str, str]] = {"height": "geom_dem", "incidence": "geom_inc"}
"""Each geometry raster gets its own subdirectory.

`prep_roipac` accepts only a fixed list of extensions, and `.hgt` is the one that passes
through untouched, so every geometry file has to be named `.hgt`. Separate directories keep
the `mintpy.load.*File` globs from colliding.
"""


def prepare_geometry(pair_dir: Path, out_dir: Path) -> dict[str, Path]:
    """Write the stack's shared geometry — height and incidence angle — as ROI_PAC `.hgt`.

    The incidence angle needs a conversion that is easy to get backwards. HyP3's `lv_theta` is
    the look vector's **elevation above the horizontal** in radians — median 1.004 rad, i.e.
    57.5 degrees, over this AOI — whereas MintPy wants the incidence angle from vertical in
    degrees. So `incidence = 90 - degrees(lv_theta)`, which gives 32.5 degrees here and agrees
    with the 32.9-degree nominal mid-swath value for IW1. MintPy applies exactly this
    conversion itself when reading a HyP3 product whose `.rsc` says `UNIT: radian`; it is done
    here instead because these files are relabelled ROI_PAC and would not trigger it.
    """
    from mintpy.prep_hyp3 import add_hyp3_metadata

    dem_tif = next(iter(pair_dir.glob(f"*{DEM_SUFFIX}")), None)
    lv_theta_tif = next(iter(pair_dir.glob(f"*{LV_THETA_SUFFIX}")), None)
    txt = next(iter(pair_dir.glob("*.txt")), None)
    if dem_tif is None or lv_theta_tif is None or txt is None:
        raise SeracError(f"{pair_dir} has no DEM crop, look-vector crop or metadata file")
    product = dem_tif.name[: -len(DEM_SUFFIX)]

    elevation_rad = _read_band(lv_theta_tif)
    incidence_deg = np.where(
        elevation_rad == 0.0, np.float32("nan"), 90.0 - np.degrees(elevation_rad)
    ).astype(np.float32)

    rasters = {"height": _read_band(dem_tif), "incidence": incidence_deg}
    units = {"height": "m", "incidence": "degree"}
    out: dict[str, Path] = {}
    for name, data in rasters.items():
        target_dir = out_dir / GEOMETRY_SUBDIRS[name]
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(txt, target_dir / f"{product}.txt")
        dest = write_bil_pair(data, data, target_dir / f"{product}.hgt")
        meta = add_hyp3_metadata(str(dest), dict(raster_metadata(dem_tif)), is_ifg=False)
        write_rsc(
            _finalise(meta, file_type=".hgt", unit=units[name]),
            dest.with_suffix(dest.suffix + ".rsc"),
        )
        out[name] = dest
    return out


def prepare_stack(data_dir: Path, aoi_id: str, *, force: bool = False) -> dict[str, Any]:
    """Convert every harvested pair. Skips pairs already converted unless `force`."""
    source_root = data_dir / "raw" / "hyp3_burst_insar" / aoi_id
    out_dir = mintpy_inputs_dir(data_dir, aoi_id)
    pair_dirs = sorted(p for p in source_root.glob("S1_*") if p.is_dir())
    if not pair_dirs:
        raise SeracError(f"no harvested pairs under {source_root}")
    prepared: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for pair_dir in pair_dirs:
        unw_tif = next(iter(pair_dir.glob(f"*{UNW_SUFFIX}")), None)
        if unw_tif is None:
            failed.append({"pair": pair_dir.name, "error": "no unw_phase crop"})
            continue
        product = unw_tif.name[: -len(UNW_SUFFIX)]
        if not force and (out_dir / f"{product}.unw.rsc").exists():
            skipped.append(product)
            continue
        try:
            prepare_pair(pair_dir, out_dir)
        except Exception as exc:
            failed.append({"pair": pair_dir.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        prepared.append(product)
    geometry = prepare_geometry(pair_dirs[0], out_dir)
    summary = {
        "aoi_id": aoi_id,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "inputs_dir": out_dir.as_posix(),
        "n_pairs_prepared": len(prepared),
        "n_pairs_skipped": len(skipped),
        "n_pairs_failed": len(failed),
        "failed": failed[:20],
        "geometry_files": {k: v.as_posix() for k, v in geometry.items()},
        "format": (
            "ROI_PAC two-band BIL float32 (.unw/.cor/.hgt) with .rsc sidecars, because MintPy's "
            "hyp3 reader needs GDAL Python bindings that are not installable here"
        ),
    }
    (out_dir / "prepare_stack.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return summary
