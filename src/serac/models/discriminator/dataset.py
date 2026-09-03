"""Zarr store for the discriminator's windows, plus the chunk-hash index that proves it.

Layout (Zarr v3, matching the feature cube's choice in ADR-0003):

    waveform  (n_windows, 12, 3, 12000) float32   velocity in m/s, 0.005-5 Hz, 20 Hz
    valid     (n_windows, 12, 3)        bool      True where a real, response-removed trace sits

`valid` is the whole point of storing a mask rather than NaNs: "this station recorded nothing"
and "this station recorded zero ground velocity" are different facts, and a feature computed
over a padded zero would silently become an observation. Every feature in `features.py` reads
`valid` before it reads `waveform`.

The station axis is fixed at 12 and padded, so an event with four usable stations has eight
all-false rows. Nothing may read a padded row, and the station axis carries **no order**: the
deep model's station-axis attention has no positional encoding precisely so it cannot learn
"slot 0 is the nearest station", which would be geometry leaking in through the back door.

**The chunk-hash index.** Zarr writes many files and a directory tree's mtimes are not
evidence of anything. After a build, every file in the store is hashed, the (path, sha256,
size) triples are sorted by path and written to `chunk_hashes.tsv`, and the sha256 *of that
file* goes in the ledger. One 64-character string in `data/manifest.jsonl` then pins the exact
bytes of a multi-gigabyte store, and `verify_store` re-derives it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator.catalog import CatalogEntry, ClassLabel
from serac.models.discriminator.windows import (
    COMPONENTS,
    MAX_STATIONS_PER_EVENT,
    N_SAMPLES,
    StationChoice,
)

DATASET_VERSION = "0.1.0"

CHUNK_INDEX_NAME: Final = "chunk_hashes.tsv"
WINDOW_INDEX_NAME: Final = "windows.json"
ZARR_NAME: Final = "windows.zarr"


class DatasetError(SeracError):
    """The discriminator dataset could not be written or read."""


class WindowRecord(BaseModel):
    """One row of the window index: everything about a window that is not a sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, description="Row in the Zarr arrays.")
    entry_id: str = Field(min_length=1)
    event_group: str = Field(min_length=1)
    class_label: ClassLabel
    origin_utc: AwareDatetime
    region_id: str
    decade: str
    source: str
    source_ids: list[str]
    magnitude: float | None = None
    sub_type: str | None = None
    matched_positive_id: str | None = None
    station_keys: list[str] = Field(
        description="net.sta.loc.band per occupied station slot, in slot order."
    )
    n_stations: int = Field(ge=0)
    n_valid_channels: int = Field(ge=0)
    description: str = ""


class DatasetIndex(BaseModel):
    """The window index and the provenance of the build that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: str = DATASET_VERSION
    built_at_utc: AwareDatetime
    n_windows: int = Field(ge=0)
    max_stations: int = MAX_STATIONS_PER_EVENT
    n_components: int = len(COMPONENTS)
    n_samples: int = N_SAMPLES
    sampling_rate_hz: float
    bandpass_hz: tuple[float, float]
    components: list[str] = Field(default_factory=lambda: list(COMPONENTS))
    windows: list[WindowRecord]
    notes: list[str] = Field(default_factory=list)

    def groups(self) -> set[str]:
        return {w.event_group for w in self.windows}


def open_store(
    root: Path, *, n_windows: int, mode: Literal["r", "r+", "a", "w", "w-"] = "w"
) -> Any:
    """Create or open the Zarr group holding `waveform` and `valid`."""
    import zarr

    store = zarr.open_group(str(root / ZARR_NAME), mode=mode)
    if mode == "w":
        store.create_array(
            "waveform",
            shape=(n_windows, MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES),
            chunks=(1, MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES),
            dtype="float32",
            fill_value=0.0,
        )
        store.create_array(
            "valid",
            shape=(n_windows, MAX_STATIONS_PER_EVENT, len(COMPONENTS)),
            chunks=(n_windows, MAX_STATIONS_PER_EVENT, len(COMPONENTS)),
            dtype="bool",
            fill_value=False,
        )
    return store


def write_window(
    store: Any,
    index: int,
    waveform: np.ndarray,
    valid: np.ndarray,
) -> None:
    """Write one window's (12, 3, 12000) block and its (12, 3) mask."""
    if waveform.shape != (MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES):
        raise DatasetError(f"window {index}: waveform shape {waveform.shape} is wrong")
    store["waveform"][index] = waveform.astype(np.float32)
    store["valid"][index] = valid.astype(bool)


def make_record(
    index: int,
    entry: CatalogEntry,
    stations: list[StationChoice],
    valid: np.ndarray,
) -> WindowRecord:
    return WindowRecord(
        index=index,
        entry_id=entry.entry_id,
        event_group=entry.event_group,
        class_label=entry.class_label,
        origin_utc=entry.origin_utc,
        region_id=entry.region_id,
        decade=entry.decade,
        source=entry.source.value,
        source_ids=list(entry.source_ids),
        magnitude=entry.magnitude,
        sub_type=entry.sub_type,
        matched_positive_id=entry.matched_positive_id,
        station_keys=[s.key for s in stations],
        n_stations=len(stations),
        n_valid_channels=int(valid.sum()),
        description=entry.description,
    )


def write_index(root: Path, index: DatasetIndex) -> Path:
    path = root / WINDOW_INDEX_NAME
    path.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_index(root: Path) -> DatasetIndex:
    path = root / WINDOW_INDEX_NAME
    if not path.exists():
        raise DatasetError(f"no window index at {path}; build the dataset first")
    return DatasetIndex.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def write_chunk_index(root: Path) -> tuple[Path, str, int]:
    """Hash every file in the Zarr store into a sorted TSV; return (path, its sha256, n files).

    Sorting by POSIX path is what makes the index reproducible: a rebuild that produces the
    same bytes produces the same TSV and therefore the same one-line ledger hash, whatever
    order the filesystem happened to hand the files back in.
    """
    zarr_root = root / ZARR_NAME
    if not zarr_root.exists():
        raise DatasetError(f"no Zarr store at {zarr_root}")
    rows = []
    for path in sorted(p for p in zarr_root.rglob("*") if p.is_file()):
        checksum, size = _sha256_file(path)
        rows.append(f"{path.relative_to(zarr_root).as_posix()}\t{checksum}\t{size}")
    index_path = root / CHUNK_INDEX_NAME
    index_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return index_path, _sha256_file(index_path)[0], len(rows)


def verify_store(root: Path) -> tuple[bool, list[str]]:
    """Re-hash the store and compare it to the committed chunk index. (ok, differences)."""
    index_path = root / CHUNK_INDEX_NAME
    if not index_path.exists():
        return False, [f"no chunk index at {index_path}"]
    expected: dict[str, tuple[str, int]] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, checksum, size_text = line.split("\t")
        expected[name] = (checksum, int(size_text))
    zarr_root = root / ZARR_NAME
    differences = []
    seen = set()
    for path in sorted(p for p in zarr_root.rglob("*") if p.is_file()):
        name = path.relative_to(zarr_root).as_posix()
        seen.add(name)
        if name not in expected:
            differences.append(f"unindexed file: {name}")
            continue
        checksum, size = _sha256_file(path)
        if (checksum, size) != expected[name]:
            differences.append(f"changed: {name}")
    differences.extend(f"missing: {name}" for name in sorted(set(expected) - seen))
    return not differences, differences


def load_arrays(root: Path) -> tuple[Any, Any]:
    """(waveform, valid) as lazy Zarr arrays."""
    import zarr

    store = zarr.open_group(str(root / ZARR_NAME), mode="r")
    return store["waveform"], store["valid"]
