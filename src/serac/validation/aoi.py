"""`validate-aoi`: every committed AOI directory is what its models and sources say it is.

Checks per AOI (all `error` unless stated):

* the seven files exist and parse through their models (`AOI`, `GridSpec`, `Transect`,
  `ExposedAsset`); GeoJSON is RFC 7946 (WGS 84 lon/lat, no `crs` member, ordered positions);
* `grid.json` equals the `GridSpec` recomputed from `aoi.json`'s extent and `AOI.grid`;
* every feature carries non-empty `source_refs` that resolve to `aoi.json` `sources[]`, a
  `geometry_quality` and a `positional_accuracy_m`;
* every `ExposedAsset.capacity_mw`/`population` with a `best` cites a qualifying source kind;
* the centreline is a single LineString or MultiLineString whose chainage is monotonic and
  whose projected length matches its declared chainage; transects lie on it and their
  chainages are ordered along it;
* transect and asset ids are unique;
* hand-digitised geometry is listed in a `warning` check so it is visible, never hidden.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from serac.domain.common import BEST_QUALIFYING_KINDS, GeometryQuality, Range, SourceRef
from serac.domain.geometry import LineString as GeoLineString
from serac.domain.geometry import Point as GeoPoint
from serac.domain.geometry import Polygon as GeoPolygon
from serac.pipelines.aoi_build import (
    AOI_FILES,
    AoiFiles,
    FeatureType,
    features_of,
    geometry_to_shapely,
    grid_from_bbox,
    iter_aoi_dirs,
    project_to,
    read_aoi_dir,
    transformers,
)
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "aoi"
LENGTH_TOLERANCE = 0.01
TRANSECT_ON_LINE_TOLERANCE_M = 5.0


def _finite(values: Any) -> bool:
    if isinstance(values, int | float):
        return math.isfinite(values)
    if isinstance(values, list | tuple):
        return all(_finite(v) for v in values)
    return False


def _check_geojson_shape(suite: Suite, aoi_id: str, name: str, doc: dict[str, Any]) -> bool:
    ok = doc.get("type") == "FeatureCollection" and "crs" not in doc
    feats = doc.get("features")
    if not isinstance(feats, list) or not feats:
        ok = False
    else:
        for f in feats:
            geom = f.get("geometry") if isinstance(f, dict) else None
            props = f.get("properties") if isinstance(f, dict) else None
            if not isinstance(geom, dict) or not isinstance(props, dict):
                ok = False
                break
            if not _finite(geom.get("coordinates")):
                ok = False
                break
    return suite.check(f"{aoi_id}:{name}:rfc7946", ok, "FeatureCollection, no crs, finite lon/lat")


def _check_feature_props(
    suite: Suite, aoi_id: str, name: str, doc: dict[str, Any], sources: dict[str, SourceRef]
) -> list[dict[str, Any]]:
    hand: list[str] = []
    problems: list[str] = []
    for i, f in enumerate(features_of(doc)):
        props = f["properties"]
        label = f"{name}[{i}]"
        ftype = props.get("feature_type")
        if ftype not in {t.value for t in FeatureType}:
            problems.append(f"{label}: feature_type={ftype!r}")
        refs = props.get("source_refs")
        if not isinstance(refs, list) or not refs:
            problems.append(f"{label}: empty source_refs")
        else:
            for r in refs:
                if r not in sources:
                    problems.append(f"{label}: source_refs {r!r} not in aoi.json sources[]")
        quality = props.get("geometry_quality")
        if quality not in {q.value for q in GeometryQuality}:
            problems.append(f"{label}: geometry_quality={quality!r}")
        elif quality in {
            GeometryQuality.hand_digitised_approximate.value,
            GeometryQuality.source_stated_location.value,
        }:
            hand.append(f"{label} ({props.get('id') or ftype})")
        acc = props.get("positional_accuracy_m")
        if not isinstance(acc, int | float) or not math.isfinite(acc) or acc < 0:
            problems.append(f"{label}: positional_accuracy_m={acc!r}")
    suite.check(f"{aoi_id}:{name}:provenance", not problems, "; ".join(problems[:6]))
    return [{"label": h} for h in hand]


def _check_geometry_type(
    suite: Suite, aoi_id: str, name: str, doc: dict[str, Any], allowed: tuple[str, ...]
) -> None:
    types = [f["geometry"].get("type") for f in features_of(doc)]
    ok = len(types) == 1 and types[0] in allowed
    suite.check(f"{aoi_id}:{name}:geometry", ok, f"one feature of {allowed}, got {types}")


def _validate_polygon(doc: dict[str, Any]) -> str | None:
    try:
        GeoPolygon.model_validate(features_of(doc)[0]["geometry"])
    except (ValidationError, IndexError, KeyError) as exc:
        return str(exc)[:200]
    return None


def _centreline_checks(suite: Suite, files: AoiFiles) -> None:
    aoi = files.aoi
    feats = features_of(files.centreline)
    if len(feats) != 1:
        suite.check(f"{aoi.id}:centreline:single", False, f"{len(feats)} features")
        return
    geom = feats[0]["geometry"]
    props = feats[0]["properties"]
    gtype = geom.get("type")
    parts: list[list[Any]]
    if gtype == "LineString":
        parts = [geom["coordinates"]]
    elif gtype == "MultiLineString":
        parts = list(geom["coordinates"])
    else:
        suite.check(f"{aoi.id}:centreline:single", False, f"geometry type {gtype!r}")
        return
    try:
        for p in parts:
            GeoLineString.model_validate({"type": "LineString", "coordinates": p})
    except ValidationError as exc:
        suite.check(f"{aoi.id}:centreline:single", False, str(exc)[:200])
        return
    suite.check(f"{aoi.id}:centreline:single", True, f"{gtype} with {len(parts)} part(s)")

    to_proj, _ = transformers(aoi.cube_epsg)
    total = 0.0
    monotonic = True
    for part in parts:
        prev: tuple[float, float] | None = None
        for lon, lat in ((c[0], c[1]) for c in part):
            xy = to_proj.transform(lon, lat)
            if prev is not None:
                step = math.hypot(xy[0] - prev[0], xy[1] - prev[1])
                if step <= 0.0:
                    monotonic = False
                total += step
            prev = xy
    suite.check(
        f"{aoi.id}:centreline:monotonic_chainage",
        monotonic,
        "no zero-length or repeated vertices along the centreline",
    )
    declared = props.get("chainage_km")
    ok_len = False
    detail = f"declared {declared!r}"
    if isinstance(declared, dict):
        start, end = declared.get("start"), declared.get("end")
        if isinstance(start, int | float) and isinstance(end, int | float):
            declared_m = (end - start) * 1000.0
            ok_len = abs(declared_m - total) <= LENGTH_TOLERANCE * max(total, 1.0)
            detail = f"declared {declared_m / 1000:.3f} km, measured {total / 1000:.3f} km"
    suite.check(f"{aoi.id}:centreline:length_matches_chainage", ok_len, detail)

    line = geometry_to_shapely(geom)
    line_proj = project_to(line, aoi.cube_epsg)
    last = -1.0
    ordered = True
    on_line = True
    for t in sorted(files.transects, key=lambda t: t.chainage_km):
        pt = geometry_to_shapely(t.point)
        pt_proj = project_to(pt, aoi.cube_epsg)
        if pt_proj.distance(line_proj) > TRANSECT_ON_LINE_TOLERANCE_M:
            on_line = False
        along = float(line_proj.project(pt_proj)) / 1000.0
        if abs(along - t.chainage_km) > 0.05 or along < last:
            ordered = False
        last = along
    suite.check(
        f"{aoi.id}:transects:on_centreline",
        on_line,
        f"every transect within {TRANSECT_ON_LINE_TOLERANCE_M:.0f} m of the centreline",
    )
    suite.check(
        f"{aoi.id}:transects:chainage_consistent",
        ordered,
        "chainage_km equals the along-line distance and increases downstream",
    )


def _best_has_qualifying_source(rng: Range | None, sources: dict[str, SourceRef]) -> bool:
    if rng is None or rng.best is None:
        return True
    return any(r in sources and sources[r].kind in BEST_QUALIFYING_KINDS for r in rng.source_refs)


def validate_aoi_dir(suite: Suite, path: Path) -> None:
    aoi_id = path.name
    missing = [n for n in AOI_FILES if not (path / n).exists()]
    if not suite.check(f"{aoi_id}:files_present", not missing, f"missing {missing}"):
        return
    try:
        files = read_aoi_dir(path)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        suite.check(f"{aoi_id}:models_parse", False, str(exc)[:300])
        return
    suite.check(f"{aoi_id}:models_parse", True, "aoi.json, grid.json, transects, assets")
    aoi = files.aoi
    suite.check(f"{aoi_id}:id_matches_directory", aoi.id == aoi_id, f"aoi.id={aoi.id!r}")
    sources = {s.id for s in aoi.sources}
    source_models = {s.id: s for s in aoi.sources}

    for name in ("source_zone.geojson", "river_centreline.geojson", "corridor.geojson"):
        _check_geojson_shape(suite, aoi_id, name, files.raw[name])
    for name in ("transects.geojson", "exposed_assets.geojson"):
        doc = files.raw[name]
        ok = doc.get("type") == "FeatureCollection" and "crs" not in doc
        suite.check(f"{aoi_id}:{name}:rfc7946", ok, "FeatureCollection, no crs")

    hand: list[dict[str, Any]] = []
    for name in AOI_FILES:
        if name.endswith(".geojson"):
            hand += _check_feature_props(suite, aoi_id, name, files.raw[name], source_models)

    _check_geometry_type(suite, aoi_id, "source_zone.geojson", files.source_zone, ("Polygon",))
    _check_geometry_type(
        suite, aoi_id, "corridor.geojson", files.corridor, ("Polygon", "MultiPolygon")
    )
    for name, doc in (("source_zone", files.source_zone), ("corridor", files.corridor)):
        err = (
            _validate_polygon(doc)
            if features_of(doc)[0]["geometry"].get("type") == "Polygon"
            else None
        )
        suite.check(f"{aoi_id}:{name}:ring_valid", err is None, err or "closed rings")

    expected = grid_from_bbox(
        aoi.id, aoi.cube_epsg, aoi.cube_extent_bbox_4326, files.grid.resolution_m
    )
    suite.check(
        f"{aoi_id}:grid:recomputed",
        files.grid == expected and aoi.grid == files.grid,
        f"grid.json {files.grid.width}x{files.grid.height} @ {files.grid.resolution_m} m; "
        f"recomputed {expected.width}x{expected.height}",
    )

    _centreline_checks(suite, files)

    tids = [t.id for t in files.transects]
    suite.check(f"{aoi_id}:transects:unique_ids", len(set(tids)) == len(tids), f"{tids}")
    suite.check(
        f"{aoi_id}:transects:aoi_id",
        all(t.aoi_id == aoi.id for t in files.transects),
        "every transect carries this AOI id",
    )
    aids = [a.id for a in files.assets]
    suite.check(f"{aoi_id}:assets:unique_ids", len(set(aids)) == len(aids), f"{aids}")
    suite.check(
        f"{aoi_id}:assets:aoi_id",
        all(a.aoi_id == aoi.id for a in files.assets),
        "every asset carries this AOI id",
    )
    bad_links = [a.id for a in files.assets if a.transect_id and a.transect_id not in tids]
    suite.check(
        f"{aoi_id}:assets:transect_links", not bad_links, f"unknown transect for {bad_links}"
    )

    unsourced = [
        a.id
        for a in files.assets
        if not a.source_refs or any(r not in sources for r in a.source_refs)
    ]
    suite.check(
        f"{aoi_id}:assets:sourced",
        not unsourced,
        f"assets without a resolvable source: {unsourced}",
    )
    no_best = [
        a.id
        for a in files.assets
        if not _best_has_qualifying_source(a.capacity_mw, source_models)
        or not _best_has_qualifying_source(a.population, source_models)
    ]
    suite.check(
        f"{aoi_id}:assets:best_has_qualifying_source",
        not no_best,
        f"best without a qualifying source kind: {no_best}",
    )
    range_refs_ok = all(
        r in sources
        for a in files.assets
        for rng in (a.capacity_mw, a.population)
        if rng is not None
        for r in rng.source_refs
    )
    suite.check(
        f"{aoi_id}:assets:range_sources_resolve", range_refs_ok, "Range.source_refs in sources[]"
    )
    for t in files.transects:
        GeoPoint.model_validate(t.point.model_dump())
    suite.warn(
        f"{aoi_id}:hand_digitised_geometry",
        not hand,
        "hand-digitised/approximate features: " + ", ".join(h["label"] for h in hand),
    )
    suite.info(
        f"{aoi_id}:summary",
        f"{len(files.transects)} transects, {len(files.assets)} assets, "
        f"{len(aoi.sources)} sources, grid {files.grid.width}x{files.grid.height}",
    )


def run_suite(repo: Path) -> SuiteResult:
    suite = Suite(SUITE_NAME, repo)
    dirs = list(iter_aoi_dirs(repo))
    suite.check("aoi_directories_present", bool(dirs), f"{len(dirs)} under data/aoi/")
    for path in dirs:
        validate_aoi_dir(suite, path)
    return suite.result()
