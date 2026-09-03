#!/usr/bin/env python
"""Generate serac's labelled synthetic placeholders under `tests/fixtures/synthetic/`.

These files stand in for products that could not be fetched in the founding session because
no Earthdata Login / CDS key was available (RELEASE_STATUS.md Known gaps 10). They are
deterministic, obviously artificial patterns, tagged `SERAC_PROVENANCE=synthetic` in the
files themselves and recorded in `data/manifest.jsonl` with `provenance: synthetic`,
`status: synthetic` and a `notes` field saying why they exist. Nothing here is, or may be
described as, an observation.

* HyP3 INSAR_GAMMA pair `S1_063_20210130_20210211` for chamoli-rishiganga (a real pair name
  from the committed ASF listing: path 63, frame 492, descending, 12 days across the
  2021-02-07 event): 32x32 px at 80 m in EPSG:32644 over the Sentinel-2 fixture window,
  `*_corr.tif` (coherence, 0..1) and `*_los_disp.tif` (line-of-sight displacement, m).
* ERA5 regridding sample: 3x3 cells at 0.25 deg x 4 hourly steps of `t2m` (K) as NetCDF-3
  (the locked environment has no h5py, so NetCDF-4 cannot be written or read here), under a
  fictional AOI id so no real cube ever picks it up.

Usage: uv run python scripts/make_synthetic_fixtures.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

from serac.adapters.eo._http import sha256_and_size
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_DIR = REPO_ROOT / "tests" / "fixtures" / "synthetic"
LEDGER_PATH = REPO_ROOT / "data" / "manifest.jsonl"
ADAPTER_NAME = "synthetic-fixture"
ADAPTER_VERSION = "0.1.0"
SEED = 20260903

# HyP3 pair: real names from data/fixtures/asf/chamoli_s1_2021-01-01_2021-02-28.geojson
AOI = "chamoli-rishiganga"
PAIR_ID = "S1_063_20210130_20210211"
REFERENCE = "S1A_IW_SLC__1SDV_20210130T004341_20210130T004408_036360_04444D_13C0"
SECONDARY = "S1A_IW_SLC__1SDV_20210211T004341_20210211T004408_036535_044A5F_DDDC"
REF_TIME = datetime(2021, 1, 30, 0, 43, 41, tzinfo=UTC)
SEC_TIME = datetime(2021, 2, 11, 0, 43, 41, tzinfo=UTC)
EPSG = 32644
WINDOW = (376680.0, 3359180.0, 379240.0, 3361740.0)  # the Sentinel-2 fixture window
PIXEL_M = 80.0
SHAPE = (32, 32)
NO_CREDS_NOTE = (
    "SYNTHETIC placeholder, not an observation: no Earthdata credentials in the founding "
    "session, so no HyP3 job could be submitted (RELEASE_STATUS.md Known gaps 10). Deterministic "
    "pattern (seed 20260903) over the Sentinel-2 fixture window; the pair name is a real "
    "ASF-listed pair so the cube's time axis is honest, the pixel values are not."
)

# ERA5 sample
ERA5_AOI = "synthetic-regrid-sample"
ERA5_LATS = np.array([30.5, 30.25, 30.0])
ERA5_LONS = np.array([79.5, 79.75, 80.0])
ERA5_TIMES = np.array(
    ["2021-02-06T00", "2021-02-06T06", "2021-02-06T12", "2021-02-06T18"], dtype="datetime64[h]"
)
ERA5_NOTE = (
    "SYNTHETIC placeholder, not an observation: no CDS API key in the founding session "
    "(RELEASE_STATUS.md Known gaps 10). A 3x3x4 smooth field in kelvin used only to test the "
    "ERA5 -> 30 m regridding in pipelines/layers/era5.py under a fictional AOI id."
)


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def window_bbox_4326() -> tuple[float, float, float, float]:
    tf = Transformer.from_crs(EPSG, 4326, always_xy=True)
    w, s, e, n = WINDOW
    xs, ys = tf.transform([w, e, e, w], [s, s, n, n])
    return (min(xs), min(ys), max(xs), max(ys))


def synthetic_fields() -> tuple[np.ndarray, np.ndarray]:
    rows, cols = SHAPE
    yy, xx = np.mgrid[0:rows, 0:cols] / (rows - 1)
    rng = np.random.default_rng(SEED)
    corr = 0.35 + 0.45 * (0.5 + 0.5 * np.sin(2 * np.pi * xx) * np.cos(2 * np.pi * yy))
    corr += rng.normal(0.0, 0.02, SHAPE)
    corr = np.clip(corr, 0.0, 1.0).astype(np.float32)
    los = (0.03 * (xx - 0.5) - 0.02 * (yy - 0.5) ** 2).astype(np.float32)  # metres, +-3 cm ramp
    return corr, los


def write_tif(path: Path, data: np.ndarray, *, units: str, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    transform = rasterio.Affine(PIXEL_M, 0.0, WINDOW[0], 0.0, -PIXEL_M, WINDOW[3])
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="float32",
        count=1,
        width=SHAPE[1],
        height=SHAPE[0],
        crs=f"EPSG:{EPSG}",
        transform=transform,
        nodata=None,
        compress="deflate",
        predictor=3,
    ) as dst:
        dst.write(data, 1)
        dst.update_tags(
            SERAC_PROVENANCE="synthetic",
            SERAC_KIND=kind,
            SERAC_UNITS=units,
            SERAC_NOTES="synthetic placeholder; see data/manifest.jsonl",
            SERAC_SEED=str(SEED),
        )


def already_recorded(ledger: JsonlManifestLedger, path: Path) -> bool:
    if not path.exists():
        return False
    sha, _ = sha256_and_size(path)
    return any(e.path == rel(path) and e.sha256 == sha for e in ledger.entries())


def record(
    ledger: JsonlManifestLedger,
    *,
    source: DataSource,
    product_id: str,
    path: Path,
    aoi_id: str,
    level: str,
    notes: str,
    params: dict[str, object],
    t0: datetime | None,
    t1: datetime | None,
    bbox: tuple[float, float, float, float],
) -> None:
    if already_recorded(ledger, path):
        log(f"  already recorded: {rel(path)}")
        return
    sha, size = sha256_and_size(path)
    ledger.append(
        ManifestEntry(
            source=source,
            product_id=product_id,
            product_level=level,
            aoi_id=aoi_id,
            path=rel(path),
            url=None,
            params=params,
            sha256=sha,
            size_bytes=size,
            retrieved_at=None,
            licence="none: synthetic placeholder generated by scripts/make_synthetic_fixtures.py",
            licence_source_url=None,
            provenance=Provenance.synthetic,
            status=ManifestStatus.synthetic,
            time_start=t0,
            time_end=t1,
            bbox_4326=bbox,
            adapter=ADAPTER_NAME,
            adapter_version=ADAPTER_VERSION,
            notes=notes,
        )
    )
    log(f"  recorded {rel(path)} ({size} B, sha256 {sha[:12]})")


def make_hyp3(ledger: JsonlManifestLedger) -> None:
    log(f"[hyp3] {PAIR_ID}")
    corr, los = synthetic_fields()
    pair_dir = SYNTHETIC_DIR / "hyp3" / AOI / PAIR_ID
    bbox = window_bbox_4326()
    common = {
        "reference": REFERENCE,
        "secondary": SECONDARY,
        "pathNumber": 63,
        "frameNumber": 492,
        "flightDirection": "DESCENDING",
        "dt_days": 12.0,
        "looks": "20x4",
        "pixel_m": PIXEL_M,
        "epsg": EPSG,
        "window_bounds": list(WINDOW),
        "seed": SEED,
    }
    for suffix, data, units, kind in (
        ("corr", corr, "1", "coherence"),
        ("los_disp", los, "m", "los_displacement"),
    ):
        path = pair_dir / f"{PAIR_ID}_{suffix}.tif"
        if not path.exists():
            write_tif(path, data, units=units, kind=kind)
        record(
            ledger,
            source=DataSource.hyp3_insar,
            product_id=PAIR_ID,
            path=path,
            aoi_id=AOI,
            level="INSAR_GAMMA",
            notes=NO_CREDS_NOTE,
            params={**common, "file": suffix, "units": units},
            t0=REF_TIME,
            t1=SEC_TIME,
            bbox=bbox,
        )


def make_era5(ledger: JsonlManifestLedger) -> None:
    import xarray as xr

    log("[era5] regridding sample")
    path = SYNTHETIC_DIR / "era5" / "regrid_sample.nc"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        t = np.arange(len(ERA5_TIMES))[:, None, None]
        lat = ERA5_LATS[None, :, None]
        lon = ERA5_LONS[None, None, :]
        t2m = (
            265.0 + 3.0 * np.sin(np.pi * t / 4) - 8.0 * (lat - 30.0) + 2.0 * (lon - 79.5)
        ).astype(np.float32)
        ds = xr.Dataset(
            {
                "t2m": (
                    ("valid_time", "latitude", "longitude"),
                    t2m,
                    {"units": "K", "long_name": "2 metre temperature"},
                )
            },
            coords={
                "valid_time": ERA5_TIMES.astype("datetime64[ns]"),
                "latitude": ERA5_LATS,
                "longitude": ERA5_LONS,
            },
            attrs={"SERAC_PROVENANCE": "synthetic", "SERAC_NOTES": ERA5_NOTE, "SERAC_SEED": SEED},
        )
        ds.to_netcdf(path, engine="scipy", format="NETCDF3_CLASSIC")
    record(
        ledger,
        source=DataSource.era5_cds,
        product_id="era5-regrid-sample",
        path=path,
        aoi_id=ERA5_AOI,
        level="reanalysis",
        notes=ERA5_NOTE,
        params={"variables": ["t2m"], "grid_deg": 0.25, "shape": [4, 3, 3], "seed": SEED},
        t0=datetime(2021, 2, 6, tzinfo=UTC),
        t1=datetime(2021, 2, 6, 18, tzinfo=UTC),
        bbox=(79.5, 30.0, 80.0, 30.5),
    )


def main() -> int:
    ledger = JsonlManifestLedger(LEDGER_PATH)
    make_hyp3(ledger)
    make_era5(ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
