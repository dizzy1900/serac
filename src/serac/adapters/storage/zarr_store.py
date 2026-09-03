"""Zarr storage for feature cubes (ADR-0003: Zarr v3, unconsolidated, 1 x 512 x 512, zstd).

`ZARR_FORMAT` is the single constant the ADR allows to flip to 2 if the xarray/zarr-v3
pairing ever breaks; the roundtrip test in `tests/unit/adapters/storage/test_zarr_store.py`
is the guard. Attributes are written as plain JSON, so callers keep them to strings, numbers,
booleans, lists and null (`clean_attrs` enforces that).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from zarr.codecs import ZstdCodec

ZARR_FORMAT = 3
CHUNK_TIME = 1
CHUNK_Y = 512
CHUNK_X = 512
ZSTD_LEVEL = 3
CONSOLIDATED = False
FILL_VALUE_UINT8 = 255


def clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Coerce attribute values to JSON-native types (datetimes -> ISO strings, enums -> values)."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        out[key] = _clean(value)
    return out


def _clean(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def chunks_for(da: xr.DataArray) -> tuple[int, ...]:
    """1 x 512 x 512 (or 512 x 512) clipped to the array's own shape."""
    targets = {"time": CHUNK_TIME, "y": CHUNK_Y, "x": CHUNK_X}
    return tuple(
        max(1, min(int(da.sizes[d]), targets.get(str(d), int(da.sizes[d])))) for d in da.dims
    )


def encoding_for(ds: xr.Dataset) -> dict[str, dict[str, Any]]:
    encoding: dict[str, dict[str, Any]] = {}
    for name, da in ds.data_vars.items():
        entry: dict[str, Any] = {
            "chunks": chunks_for(da),
            "compressors": [ZstdCodec(level=ZSTD_LEVEL)],
        }
        if np.issubdtype(da.dtype, np.floating):
            entry["_FillValue"] = np.nan
        elif da.dtype == np.uint8:
            entry["_FillValue"] = FILL_VALUE_UINT8
        encoding[str(name)] = entry
    return encoding


def write_cube(ds: xr.Dataset, path: Path) -> Path:
    """Write `ds` as a Zarr v3 store at `path` (replacing anything there)."""
    cleaned = ds.copy()
    cleaned.attrs = clean_attrs(ds.attrs)
    for name in cleaned.variables:
        cleaned[name].attrs = clean_attrs(ds[name].attrs)
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_zarr(
        path,
        mode="w",
        zarr_format=ZARR_FORMAT,
        consolidated=CONSOLIDATED,
        encoding=encoding_for(cleaned),
    )
    return path


def open_cube(path: Path) -> xr.Dataset:
    """Open a cube written by `write_cube` (lazy; call `.load()` for the arrays)."""
    if not path.exists():
        raise FileNotFoundError(path)
    ds: xr.Dataset = xr.open_zarr(path, zarr_format=ZARR_FORMAT, consolidated=CONSOLIDATED)
    return ds


def store_format(path: Path) -> int | None:
    """The zarr_format declared by the store's root metadata (3 -> `zarr.json`, 2 -> `.zgroup`)."""
    if (path / "zarr.json").exists():
        return 3
    if (path / ".zgroup").exists():
        return 2
    return None
