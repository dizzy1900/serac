"""`make validate-cube`: grid/CRS consistency, monotonic UTC time, provenance attrs, STAC.

`run_suite(repo, cube_path)` validates one store. With no `cube_path` it looks for
`data/features/<aoi>/cube.zarr`; when none exists it builds the Chamoli acceptance cube from
the committed fixtures into `reports/cube/_validate/` (gitignored) and validates that, so the
suite is meaningful on a fresh clone with no DVC remote. Every rule is a named check.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import xarray as xr

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.adapters.storage.stac_catalog import read_stac, validate_stac
from serac.adapters.storage.zarr_store import ZARR_FORMAT, open_cube, store_format
from serac.domain.geo import GridSpec
from serac.domain.manifest import ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.build_cube import (
    CUBE_DIRNAME,
    REQUIRED_LAYERS,
    STAC_DIRNAME,
    TEMPORAL_LAYERS,
    CubeBuildReport,
    build_cube,
    resolve_cube_aoi,
)
from serac.pipelines.grid import grid_path, grids_equal, load_grid
from serac.pipelines.layers._base import REQUIRED_LAYER_ATTRS, VALID_SUFFIX
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "cube"
DEFAULT_AOI = "chamoli-rishiganga"
FALLBACK_BBOX = (79.68, 30.33, 79.80, 30.42)  # the fixture design bbox (FIXTURES.md)
FALLBACK_EPSG = 32644
FALLBACK_WINDOW = (datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 2, 15, 23, 59, 59, tzinfo=UTC))
VALIDATE_DIRNAME = "_validate"
SCHEMAS_REL = Path("tests/fixtures/stac_schemas")
ALLOWED_STATUS = {"fetched", "partial", "synthetic", "not_fetched"}
ALLOWED_PROVENANCE = {"real", "synthetic", "none"}


def _rel(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix() if path.is_relative_to(repo) else path.as_posix()


def default_cube_path(repo: Path, aoi_id: str) -> Path:
    return repo / "data" / "features" / aoi_id / CUBE_DIRNAME


def build_fixture_cube(repo: Path, aoi_id: str = DEFAULT_AOI) -> Path:
    """Build the acceptance cube from `data/fixtures` into `reports/cube/_validate/<aoi>/`."""
    data_dir = repo / "data"
    try:
        target = resolve_cube_aoi(data_dir, aoi_id)
    except ValueError:
        target = resolve_cube_aoi(data_dir, aoi_id, bbox=FALLBACK_BBOX, epsg=FALLBACK_EPSG)
    out = repo / "reports" / "cube" / VALIDATE_DIRNAME / aoi_id
    build_cube(
        target,
        *FALLBACK_WINDOW,
        raw_root=data_dir / "fixtures",
        ledger=JsonlManifestLedger(data_dir / "manifest.jsonl"),
        out=out,
        repo_root=repo,
        reports_dir=out / "reports",
    )
    return out / CUBE_DIRNAME


def _ledger_index(repo: Path) -> tuple[dict[str, list[ManifestEntry]], dict[str, ManifestEntry]]:
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    by_product: dict[str, list[ManifestEntry]] = {}
    by_id: dict[str, ManifestEntry] = {}
    for e in ledger.entries():
        by_product.setdefault(e.product_id, []).append(e)
        by_id[e.entry_id] = e
    return by_product, by_id


def run_suite(
    repo: Path,
    cube_path: Path | None = None,
    *,
    aoi_id: str | None = None,
    schemas_dir: Path | None = None,
    build_if_missing: bool = True,
) -> SuiteResult:
    suite = Suite(SUITE_NAME, repo)
    aoi = aoi_id or DEFAULT_AOI
    path = cube_path or default_cube_path(repo, aoi)
    if not path.exists() and cube_path is None and build_if_missing:
        try:
            path = build_fixture_cube(repo, aoi)
            suite.info(
                "cube.built_from_fixtures", f"no {default_cube_path(repo, aoi)}; built {path}"
            )
        except Exception as exc:
            suite.check("cube.built_from_fixtures", False, f"{type(exc).__name__}: {exc}")
            return suite.result()
    if not suite.check("cube.exists", path.exists(), str(path)):
        return suite.result()
    suite.check(
        "cube.zarr_format",
        store_format(path) == ZARR_FORMAT,
        f"store declares zarr_format {store_format(path)}, expected {ZARR_FORMAT}",
    )
    try:
        ds = open_cube(path).load()
    except Exception as exc:
        suite.check("cube.opens", False, f"{type(exc).__name__}: {exc}")
        return suite.result()
    suite.check("cube.opens", True, f"{len(ds.data_vars)} variables")
    cube_aoi = str(ds.attrs.get("aoi_id", aoi))
    _check_grid(suite, repo, ds, cube_aoi)
    _check_time(suite, ds)
    _check_layers(suite, ds)
    _check_ledger_links(suite, repo, ds, cube_aoi)
    _check_stac(suite, repo, path, ds, schemas_dir)
    _check_report(suite, repo, path, ds, cube_aoi)
    return suite.result()


def _grid_from_attrs(ds: xr.Dataset) -> GridSpec | None:
    raw = ds.attrs.get("grid")
    if not isinstance(raw, dict):
        return None
    try:
        return GridSpec.model_validate(raw)
    except ValueError:
        return None


def _check_grid(suite: Suite, repo: Path, ds: xr.Dataset, aoi: str) -> None:
    grid = _grid_from_attrs(ds)
    if not suite.check("cube.grid_attr", grid is not None, "global attr `grid` parses as GridSpec"):
        return
    assert grid is not None
    wkt = str(ds["spatial_ref"].attrs.get("crs_wkt", "")) if "spatial_ref" in ds.coords else ""
    epsg_attr = ds.attrs.get("epsg")
    suite.check(
        "cube.crs",
        epsg_attr == grid.epsg and f'"EPSG",{grid.epsg}' in wkt.replace(" ", ""),
        f"attrs.epsg={epsg_attr}, grid.epsg={grid.epsg}, spatial_ref present={bool(wkt)}",
    )
    committed = grid_path(repo / "data", aoi)
    if committed.exists():
        try:
            ref = load_grid(committed)
            suite.check(
                "cube.grid_matches_committed",
                grids_equal(ref, grid),
                f"{committed.relative_to(repo)} vs cube attrs",
            )
        except ValueError as exc:
            suite.check("cube.grid_matches_committed", False, f"{committed}: {exc}")
    else:
        suite.info(
            "cube.grid_matches_committed", f"no committed grid at {committed.relative_to(repo)}"
        )
    x = np.asarray(ds["x"].values, dtype=float)
    y = np.asarray(ds["y"].values, dtype=float)
    dx = np.diff(x) if x.size > 1 else np.array([30.0])
    dy = np.diff(y) if y.size > 1 else np.array([-30.0])
    suite.check(
        "cube.resolution_30m",
        grid.resolution_m == 30.0 and np.allclose(dx, 30.0) and np.allclose(dy, -30.0),
        f"resolution_m={grid.resolution_m}, dx={dx[0]:.6f}, dy={dy[0]:.6f}",
    )
    snapped = (
        math.isclose(grid.x_min % 30.0, 0.0, abs_tol=1e-6)
        and math.isclose(grid.y_max % 30.0, 0.0, abs_tol=1e-6)
        and x.size > 0
        and math.isclose(x[0], grid.x_min + 15.0, abs_tol=1e-6)
        and math.isclose(y[0], grid.y_max - 15.0, abs_tol=1e-6)
    )
    suite.check(
        "cube.origin_snapped",
        snapped,
        f"x_min={grid.x_min}, y_max={grid.y_max}, x[0]={x[0] if x.size else 'n/a'}",
    )
    suite.check(
        "cube.shape_matches_grid",
        ds.sizes.get("x") == grid.width and ds.sizes.get("y") == grid.height,
        f"x={ds.sizes.get('x')}/{grid.width}, y={ds.sizes.get('y')}/{grid.height}",
    )


def _check_time(suite: Suite, ds: xr.Dataset) -> None:
    if "time" not in ds.coords:
        suite.check("cube.time_monotonic_utc", False, "no time coordinate")
        return
    t = np.asarray(ds["time"].values)
    is_dt = np.issubdtype(t.dtype, np.datetime64)
    strictly = (
        bool(np.all(np.diff(t.astype("datetime64[ns]").astype(np.int64)) > 0))
        if t.size > 1
        else True
    )
    tz = ds["time"].attrs.get("time_zone", "UTC")
    suite.check(
        "cube.time_monotonic_utc",
        is_dt and strictly and tz == "UTC",
        f"{t.size} steps, datetime64={is_dt}, strictly increasing={strictly}, tz={tz}",
    )


def _check_layers(suite: Suite, ds: xr.Dataset) -> None:
    missing = [n for n in REQUIRED_LAYERS if n not in ds.data_vars]
    missing += [
        f"{n}{VALID_SUFFIX}" for n in TEMPORAL_LAYERS if f"{n}{VALID_SUFFIX}" not in ds.data_vars
    ]
    suite.check(
        "cube.required_layers", not missing, f"missing: {missing}" if missing else "all present"
    )
    incomplete: list[str] = []
    for name in REQUIRED_LAYERS:
        if name not in ds.data_vars:
            continue
        attrs = ds[name].attrs
        gaps = [k for k in REQUIRED_LAYER_ATTRS if k not in attrs]
        if attrs.get("status") not in ALLOWED_STATUS:
            gaps.append(f"status={attrs.get('status')!r}")
        if attrs.get("provenance") not in ALLOWED_PROVENANCE:
            gaps.append(f"provenance={attrs.get('provenance')!r}")
        if gaps:
            incomplete.append(f"{name}: {gaps}")
    suite.check(
        "cube.layer_attrs_complete", not incomplete, "; ".join(incomplete) or "all complete"
    )
    bad_empty: list[str] = []
    for name in REQUIRED_LAYERS:
        if name in ds.data_vars and ds[name].attrs.get("status") == "not_fetched":
            all_nan = bool(ds[name].isnull().all())
            flag = f"{name}{VALID_SUFFIX}"
            never_valid = flag not in ds.data_vars or not bool(ds[flag].values.any())
            if not (all_nan and never_valid):
                bad_empty.append(name)
    suite.check(
        "cube.not_fetched_all_nan",
        not bad_empty,
        f"values in not_fetched layers: {bad_empty}" if bad_empty else "ok",
    )
    synthetic_layers = [
        n
        for n in REQUIRED_LAYERS
        if n in ds.data_vars and ds[n].attrs.get("provenance") == "synthetic"
    ]
    synthetic_flag = bool(ds.attrs.get("contains_synthetic", False))
    suite.check(
        "cube.synthetic_flag_consistent",
        synthetic_flag == bool(synthetic_layers),
        f"contains_synthetic={synthetic_flag}, synthetic layers={synthetic_layers}",
    )
    mismatches: list[str] = []
    weak: list[str] = []
    for name in TEMPORAL_LAYERS:
        flag_name = f"{name}{VALID_SUFFIX}"
        if name not in ds.data_vars or flag_name not in ds.data_vars:
            continue
        valid = np.asarray(ds[flag_name].values, dtype=bool)
        null = ds[name].isnull().values
        for i, ok in enumerate(valid):
            slice_all_null = bool(null[i].all()) if null.ndim == 3 else True
            if not ok and not slice_all_null:
                mismatches.append(f"{name}[{i}] invalid but has data")
            if ok and slice_all_null:
                weak.append(f"{name}[{i}] valid but all NaN")
    suite.check("cube.valid_mask_matches_data", not mismatches, "; ".join(mismatches) or "ok")
    suite.warn("cube.valid_slices_have_data", not weak, "; ".join(weak) or "ok")


def _check_ledger_links(suite: Suite, repo: Path, ds: xr.Dataset, aoi: str) -> None:
    by_product, by_id = _ledger_index(repo)
    unresolved: list[str] = []
    wrong_provenance: list[str] = []
    for name in REQUIRED_LAYERS:
        if name not in ds.data_vars:
            continue
        attrs = ds[name].attrs
        layer_prov = attrs.get("provenance")
        for pid in attrs.get("product_ids", []):
            rows = [
                r
                for r in by_product.get(str(pid), [])
                if r.status in (ManifestStatus.fetched, ManifestStatus.synthetic)
            ]
            if not rows:
                unresolved.append(f"{name}:{pid}")
                continue
            provs = {r.provenance for r in rows}
            if layer_prov == "synthetic" and Provenance.synthetic not in provs:
                wrong_provenance.append(f"{name}:{pid} synthetic layer, real rows")
            if layer_prov == "real" and Provenance.synthetic in provs:
                wrong_provenance.append(f"{name}:{pid} real layer, synthetic rows")
        for eid in attrs.get("manifest_entry_ids", []):
            if str(eid) not in by_id:
                unresolved.append(f"{name}:entry {eid}")
    suite.check(
        "cube.product_ids_resolve",
        not unresolved,
        "; ".join(unresolved) or "every product id and entry id has a fetched/synthetic ledger row",
    )
    suite.check(
        "cube.provenance_matches_ledger", not wrong_provenance, "; ".join(wrong_provenance) or "ok"
    )


def _check_stac(
    suite: Suite, repo: Path, path: Path, ds: xr.Dataset, schemas_dir: Path | None
) -> None:
    stac_dir = path.parent / STAC_DIRNAME
    if not suite.check("cube.stac_present", (stac_dir / "catalog.json").exists(), str(stac_dir)):
        return
    try:
        _catalog, collection, items = read_stac(stac_dir)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        suite.check("cube.stac_reads", False, f"{type(exc).__name__}: {exc}")
        return
    n_time = int(ds.sizes.get("time", 0))
    suite.check(
        "cube.stac_items_match_time",
        len(items) == n_time,
        f"{len(items)} items vs {n_time} time steps",
    )
    suite.check(
        "cube.stac_synthetic_flag",
        bool(collection.get("serac:contains_synthetic"))
        == bool(ds.attrs.get("contains_synthetic")),
        f"collection={collection.get('serac:contains_synthetic')}, "
        f"cube={ds.attrs.get('contains_synthetic')}",
    )
    schemas = schemas_dir or (repo / SCHEMAS_REL)
    if schemas.exists():
        problems = validate_stac(stac_dir, schemas)
        suite.check(
            "cube.stac_schema_valid",
            not problems,
            "; ".join(problems[:5]) or "valid against STAC 1.1.0 core schemas",
        )
    else:
        suite.warn("cube.stac_schema_valid", False, f"no vendored schemas at {schemas}")


def _check_report(suite: Suite, repo: Path, path: Path, ds: xr.Dataset, aoi: str) -> None:
    candidates = [
        path.parent / "reports" / f"{aoi}.json",
        repo / "reports" / "cube" / f"{aoi}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                report = CubeBuildReport.model_validate_json(candidate.read_text("utf-8"))
            except ValueError as exc:
                suite.check("cube.report_parses", False, f"{candidate}: {exc}")
                return
            suite.check(
                "cube.report_parses",
                report.n_times == int(ds.sizes.get("time", 0))
                and report.contains_synthetic == bool(ds.attrs.get("contains_synthetic")),
                f"{_rel(candidate, repo)}: n_times={report.n_times}",
            )
            return
    suite.warn(
        "cube.report_parses", False, f"no build report at {candidates[0]} or {candidates[1]}"
    )
