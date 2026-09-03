"""STAC catalogue for feature cubes (ADR-0003): Catalog -> Collection per AOI -> Item per slice.

`write_stac` publishes a self-contained catalogue next to the Zarr store. `validate_stac`
checks every JSON document against the STAC 1.1.0 core schemas (and the GeoJSON schemas they
reference) vendored under `tests/fixtures/stac_schemas/`, resolved through a local
`referencing.Registry`, so validation never touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pystac
import xarray as xr
from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from serac import __version__
from serac.domain.geo import GridSpec
from serac.pipelines.grid import grid_bounds_4326

STAC_VERSION = "1.1.0"
CATALOG_ID = "serac"
CATALOG_FILENAME = "catalog.json"
CUBE_MEDIA_TYPE = "application/vnd+zarr"
SCHEMA_BASE = "https://schemas.stacspec.org/v1.1.0/"
ITEM_SCHEMA_URI = SCHEMA_BASE + "item-spec/json-schema/item.json"
COLLECTION_SCHEMA_URI = SCHEMA_BASE + "collection-spec/json-schema/collection.json"
CATALOG_SCHEMA_URI = SCHEMA_BASE + "catalog-spec/json-schema/catalog.json"
DRAFT7_URI = "http://json-schema.org/draft-07/schema"
VALID_SUFFIX = "_valid"


def collection_id(aoi_id: str) -> str:
    return f"serac-cube-{aoi_id}"


def item_id(aoi_id: str, when: datetime) -> str:
    return f"{collection_id(aoi_id)}-{when:%Y%m%dT%H%M%SZ}"


def _to_datetime(value: Any) -> datetime:
    ts = np.datetime64(value, "us").astype("datetime64[us]").astype(object)
    if not isinstance(ts, datetime):
        raise TypeError(f"not a time coordinate: {value!r}")
    return ts.replace(tzinfo=UTC)


def cube_times(ds: xr.Dataset) -> list[datetime]:
    if "time" not in ds.coords:
        return []
    return [_to_datetime(v) for v in ds["time"].values]


def layers_present_at(ds: xr.Dataset, index: int) -> list[str]:
    """Temporal layers whose `<layer>_valid` flag is set at time `index`."""
    present: list[str] = []
    for name in ds.data_vars:
        layer = str(name)
        if layer.endswith(VALID_SUFFIX):
            continue
        flag = f"{layer}{VALID_SUFFIX}"
        if flag in ds.data_vars and "time" in ds[flag].dims and bool(ds[flag].values[index]):
            present.append(layer)
    return sorted(present)


def bbox_polygon(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    w, s, e, n = bbox
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


@dataclass(frozen=True)
class StacPaths:
    catalog: Path
    collection: Path
    items: tuple[Path, ...]


def write_stac(
    ds: xr.Dataset,
    *,
    aoi_id: str,
    grid: GridSpec,
    out_dir: Path,
    cube_href: str,
    built_at: datetime | None = None,
) -> StacPaths:
    """Publish catalog.json, the AOI collection and one item per time slice under `out_dir`."""
    built_at = built_at or datetime.now(tz=UTC)
    bbox = grid_bounds_4326(grid)
    times = cube_times(ds)
    if times:
        interval: list[datetime | None] = [min(times), max(times)]
    else:
        interval = [built_at, built_at]
    static_layers = sorted(
        str(n)
        for n in ds.data_vars
        if "time" not in ds[n].dims and not str(n).endswith(VALID_SUFFIX)
    )
    temporal_layers = sorted(
        str(n) for n in ds.data_vars if "time" in ds[n].dims and not str(n).endswith(VALID_SUFFIX)
    )
    catalog = pystac.Catalog(
        id=CATALOG_ID,
        description="serac feature cubes: one collection per AOI, one item per time slice.",
        title="serac",
    )
    collection = pystac.Collection(
        id=collection_id(aoi_id),
        description=f"serac feature cube for AOI {aoi_id} on a {grid.resolution_m:g} m grid "
        f"(EPSG:{grid.epsg}).",
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(bbox)]),
            temporal=pystac.TemporalExtent([interval]),
        ),
        license="other",
        title=f"serac cube {aoi_id}",
        extra_fields={
            "serac:grid": json.loads(grid.model_dump_json()),
            "serac:contains_synthetic": bool(ds.attrs.get("contains_synthetic", False)),
            "serac:cube_schema_version": str(ds.attrs.get("cube_schema_version", "")),
            "serac:static_layers": static_layers,
            "serac:temporal_layers": temporal_layers,
            "serac:built_at": built_at.isoformat(),
            "serac:serac_version": __version__,
        },
    )
    collection.add_asset(
        "cube",
        pystac.Asset(
            href=cube_href,
            media_type=CUBE_MEDIA_TYPE,
            roles=["data"],
            title="Zarr feature cube",
            extra_fields={"zarr:format": int(ds.attrs.get("zarr_format", 3))},
        ),
    )
    catalog.add_child(collection)
    for index, when in enumerate(times):
        layers = layers_present_at(ds, index)
        item = pystac.Item(
            id=item_id(aoi_id, when),
            geometry=bbox_polygon(bbox),
            bbox=list(bbox),
            datetime=when,
            properties={
                "serac:aoi_id": aoi_id,
                "serac:layers_present": layers,
                "serac:time_index": index,
                "serac:contains_synthetic": bool(ds.attrs.get("contains_synthetic", False)),
            },
        )
        item.add_asset(
            "cube",
            pystac.Asset(
                href=cube_href,
                media_type=CUBE_MEDIA_TYPE,
                roles=["data"],
                extra_fields={"serac:time_index": index},
            ),
        )
        collection.add_item(item)
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog.normalize_and_save(str(out_dir), catalog_type=pystac.CatalogType.SELF_CONTAINED)
    catalog_path = out_dir / CATALOG_FILENAME
    collection_path = out_dir / collection.id / "collection.json"
    item_paths = tuple(
        sorted(p for p in (out_dir / collection.id).rglob("*.json") if p.name != "collection.json")
    )
    return StacPaths(catalog=catalog_path, collection=collection_path, items=item_paths)


def read_stac(out_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """(catalog, collection, items) as plain dicts from a directory written by `write_stac`."""
    catalog = json.loads((out_dir / CATALOG_FILENAME).read_text(encoding="utf-8"))
    collections = [p for p in out_dir.glob("*/collection.json")]
    if len(collections) != 1:
        raise ValueError(
            f"expected exactly one collection under {out_dir}, found {len(collections)}"
        )
    collection = json.loads(collections[0].read_text(encoding="utf-8"))
    items = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(collections[0].parent.rglob("*.json"))
        if p.name != "collection.json"
    ]
    return catalog, collection, items


# -- offline validation ----------------------------------------------------------------------


def schema_registry(schemas_dir: Path) -> Registry[Any]:
    """A `referencing` registry of the vendored schemas, keyed by their `$id` and their URL.

    Both keys matter: upstream `common.json` declares `$id: .../commonjson` (a typo in STAC
    1.1.0) while `item.json` references it as `common.json`, so the retrieval URL recorded in
    `MANIFEST.json` is what relative `$ref`s actually resolve to.
    """
    manifest = json.loads((schemas_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    urls = {row["file"]: str(row["url"]) for row in manifest["files"]}
    resources: list[tuple[str, Resource[Any]]] = []
    for path in sorted(schemas_dir.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc, default_specification=DRAFT7)
        for uri in {doc.get("$id"), urls.get(path.name)}:
            if uri:
                resources.append((uri, resource))
    meta = Draft7Validator.META_SCHEMA
    resources.append((DRAFT7_URI, Resource.from_contents(meta, default_specification=DRAFT7)))
    resources.append((DRAFT7_URI + "#", Resource.from_contents(meta, default_specification=DRAFT7)))
    return Registry().with_resources(resources)


def validate_document(doc: dict[str, Any], schema_uri: str, registry: Registry[Any]) -> list[str]:
    schema = registry.contents(schema_uri)
    validator = Draft7Validator(schema, registry=registry)
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]


def validate_stac(out_dir: Path, schemas_dir: Path) -> list[str]:
    """Problems found in the catalogue (empty list == valid)."""
    registry = schema_registry(schemas_dir)
    problems: list[str] = []
    catalog, collection, items = read_stac(out_dir)
    for label, doc, uri in (
        ("catalog", catalog, CATALOG_SCHEMA_URI),
        ("collection", collection, COLLECTION_SCHEMA_URI),
    ):
        problems.extend(f"{label}: {p}" for p in validate_document(doc, uri, registry))
        if doc.get("stac_version") != STAC_VERSION:
            problems.append(f"{label}: stac_version {doc.get('stac_version')!r} != {STAC_VERSION}")
    for item in items:
        problems.extend(
            f"item {item.get('id')}: {p}"
            for p in validate_document(item, ITEM_SCHEMA_URI, registry)
        )
    return problems
