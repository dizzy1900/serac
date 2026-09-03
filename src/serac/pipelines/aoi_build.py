"""Build an AOI directory from OpenStreetMap hydrography (Overpass) and sourced assets.

The pipeline is deliberately mechanical so that every geometry in `data/aoi/<id>/` can be
traced to bytes that were actually retrieved:

* **Hydrography** comes from one Overpass query per AOI. The raw response is committed under
  `data/fixtures/osm/` (ODbL, attribution "© OpenStreetMap contributors") and is the `dataset`
  source of every derived line and polygon.
* **Centreline**: all `waterway=river|stream|flowline` ways (including relation members) are
  split at shared vertices into a graph; the centreline is the shortest path from the network
  node nearest the source-zone centroid (restricted to the component that reaches the
  downstream target) to the node nearest the target, clipped to the requested chainage. The
  offset between the source zone and the first network node is recorded, never hidden.
* **Corridor** and **cube extent** are buffers in the AOI's projected CRS; the `GridSpec` is a
  pure function of the WGS 84 extent so `validate-aoi` can recompute and compare it.
* **Transects and assets** are snapped or located from OSM nodes/ways, or from a location
  stated by a retrieved source (flagged `hand_digitised_approximate` with an honest accuracy).

HTTP goes through the `OverpassClient` protocol; offline builds and tests use
`FixtureOverpassClient`, which replays the committed response verbatim.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx
from pyproj import Transformer
from shapely import segmentize
from shapely.geometry import (
    LineString,
    Point,
    Polygon,
    box,
    mapping,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import substring, transform, unary_union

from serac import __version__
from serac.domain.common import GeometryQuality, Range, RecordMeta, SourceKind, SourceRef
from serac.domain.events import AssetType
from serac.domain.geo import AOI, AssetStatus, Bbox4326, ExposedAsset, GridSpec, Transect
from serac.domain.geometry import Geometry
from serac.domain.geometry import Point as GeoPoint
from serac.errors import SeracError
from serac.pipelines._geojson_io import (
    dump_geojson,
    feature,
    feature_collection,
)

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_LICENCE = "ODbL-1.0"
OSM_LICENCE_URL = "https://www.openstreetmap.org/copyright"
DEFAULT_RESOLUTION_M = 30.0
WATERWAY_KINDS: frozenset[str] = frozenset({"river", "stream", "flowline"})
ADAPTER_NAME = "aoi-build"
USER_AGENT = f"serac-aoi-build/{__version__} (+https://github.com/dizzy1900/serac)"
COORD_DECIMALS = 7

AOI_FILES: tuple[str, ...] = (
    "aoi.json",
    "source_zone.geojson",
    "river_centreline.geojson",
    "corridor.geojson",
    "transects.geojson",
    "exposed_assets.geojson",
    "grid.json",
)


class FeatureType(StrEnum):
    source_zone = "source_zone"
    river_centreline = "river_centreline"
    corridor = "corridor"
    transect = "transect"
    exposed_asset = "exposed_asset"


class AoiBuildError(SeracError):
    """The AOI could not be built from the retrieved data; nothing is invented instead."""


# --- Overpass client --------------------------------------------------------------------------


class OverpassClient(Protocol):
    """Anything that can answer an Overpass QL query with the raw response bytes."""

    def query(self, ql: str) -> bytes: ...


class HttpxOverpassClient:
    """Live Overpass API client (one POST per query, generous timeout, honest user agent)."""

    def __init__(
        self,
        endpoint: str = OVERPASS_ENDPOINT,
        timeout_s: float = 300.0,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def query(self, ql: str) -> bytes:
        response = httpx.post(
            self.endpoint,
            data={"data": ql},
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return response.content


class FixtureOverpassClient:
    """Replays a committed Overpass response; the query text is recorded but not sent."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.queries: list[str] = []

    def query(self, ql: str) -> bytes:
        self.queries.append(ql)
        return self.path.read_bytes()


# --- Overpass response model ------------------------------------------------------------------

LonLat = tuple[float, float]


@dataclass(frozen=True)
class OsmNode:
    id: int
    lon: float
    lat: float
    tags: Mapping[str, str]


@dataclass(frozen=True)
class OsmWay:
    id: int
    coords: tuple[LonLat, ...]
    tags: Mapping[str, str]

    def centroid(self) -> LonLat:
        geom: Any
        if len(self.coords) > 3 and self.coords[0] == self.coords[-1]:
            geom = Polygon(self.coords)
        elif len(self.coords) > 1:
            geom = LineString(self.coords)
        else:
            geom = Point(self.coords[0])
        c = geom.centroid
        return (float(c.x), float(c.y))


@dataclass(frozen=True)
class OverpassData:
    """Parsed Overpass JSON: nodes, ways (direct and relation members) and metadata."""

    nodes: Mapping[int, OsmNode]
    ways: Mapping[int, OsmWay]
    relation_count: int
    timestamp_osm_base: str | None
    copyright: str | None
    generator: str | None

    def waterway_ways(self, kinds: frozenset[str] = WATERWAY_KINDS) -> list[OsmWay]:
        return [w for w in self.ways.values() if w.tags.get("waterway") in kinds]

    def node(self, node_id: int) -> OsmNode:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise AoiBuildError(f"OSM node {node_id} is not in the Overpass response") from exc

    def way(self, way_id: int) -> OsmWay:
        try:
            return self.ways[way_id]
        except KeyError as exc:
            raise AoiBuildError(f"OSM way {way_id} is not in the Overpass response") from exc


def parse_overpass(raw: bytes) -> OverpassData:
    """Parse an Overpass `[out:json]` response produced with `out geom`."""
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        head = raw[:200].decode("utf-8", errors="replace")
        raise AoiBuildError(f"Overpass response is not JSON (server busy?): {head!r}") from exc
    if not isinstance(doc, dict) or "elements" not in doc:
        raise AoiBuildError("Overpass response has no 'elements'")
    nodes: dict[int, OsmNode] = {}
    ways: dict[int, OsmWay] = {}
    relation_count = 0
    for element in doc["elements"]:
        tags = {str(k): str(v) for k, v in element.get("tags", {}).items()}
        kind = element.get("type")
        if kind == "node":
            nodes[int(element["id"])] = OsmNode(
                int(element["id"]), float(element["lon"]), float(element["lat"]), tags
            )
        elif kind == "way":
            geom = element.get("geometry")
            if geom:
                coords = tuple((float(p["lon"]), float(p["lat"])) for p in geom)
                ways[int(element["id"])] = OsmWay(int(element["id"]), coords, tags)
        elif kind == "relation":
            relation_count += 1
            waterway = tags.get("waterway")
            for member in element.get("members", []):
                if member.get("type") != "way" or not member.get("geometry"):
                    continue
                ref = int(member["ref"])
                if ref in ways:
                    continue
                member_tags: dict[str, str] = {"osm:relation": str(element["id"])}
                if waterway and tags.get("type") == "waterway":
                    member_tags["waterway"] = waterway
                    if "name" in tags:
                        member_tags["name"] = tags["name"]
                coords = tuple((float(p["lon"]), float(p["lat"])) for p in member["geometry"])
                if len(coords) >= 2:
                    ways[ref] = OsmWay(ref, coords, member_tags)
    osm3s = doc.get("osm3s", {}) if isinstance(doc.get("osm3s"), dict) else {}
    return OverpassData(
        nodes=nodes,
        ways=ways,
        relation_count=relation_count,
        timestamp_osm_base=osm3s.get("timestamp_osm_base"),
        copyright=osm3s.get("copyright"),
        generator=doc.get("generator"),
    )


# --- river network ----------------------------------------------------------------------------

NodeKey = tuple[float, float]


def _key(p: LonLat) -> NodeKey:
    return (round(p[0], COORD_DECIMALS), round(p[1], COORD_DECIMALS))


@dataclass(frozen=True)
class Edge:
    way_id: int
    start: NodeKey
    end: NodeKey
    length_m: float
    coords_proj: tuple[tuple[float, float], ...]


class RiverNetwork:
    """Waterway ways split at shared vertices into an undirected weighted graph."""

    def __init__(self, ways: Sequence[OsmWay], to_proj: Transformer) -> None:
        self.to_proj = to_proj
        occurrences: dict[NodeKey, set[int]] = defaultdict(set)
        for way in ways:
            for p in way.coords:
                occurrences[_key(p)].add(way.id)
        junctions: set[NodeKey] = {k for k, s in occurrences.items() if len(s) >= 2}
        for way in ways:
            junctions.add(_key(way.coords[0]))
            junctions.add(_key(way.coords[-1]))
        self.edges: list[Edge] = []
        self.adjacency: dict[NodeKey, list[int]] = defaultdict(list)
        self.positions: dict[NodeKey, tuple[float, float]] = {}
        for way in ways:
            segment: list[LonLat] = [way.coords[0]]
            for p in way.coords[1:]:
                segment.append(p)
                if _key(p) in junctions:
                    self._add_edge(way.id, segment)
                    segment = [p]
        self._components: dict[NodeKey, int] | None = None

    def _add_edge(self, way_id: int, segment: list[LonLat]) -> None:
        proj = tuple(self.to_proj.transform(x, y) for x, y in segment)
        length = float(LineString(proj).length) if len(proj) > 1 else 0.0
        start, end = _key(segment[0]), _key(segment[-1])
        self.positions.setdefault(start, proj[0])
        self.positions.setdefault(end, proj[-1])
        idx = len(self.edges)
        self.edges.append(Edge(way_id, start, end, length, proj))
        self.adjacency[start].append(idx)
        self.adjacency[end].append(idx)

    @property
    def node_keys(self) -> list[NodeKey]:
        return list(self.positions)

    def components(self) -> dict[NodeKey, int]:
        if self._components is None:
            comp: dict[NodeKey, int] = {}
            label = 0
            for start in self.positions:
                if start in comp:
                    continue
                stack = [start]
                comp[start] = label
                while stack:
                    u = stack.pop()
                    for idx in self.adjacency[u]:
                        e = self.edges[idx]
                        v = e.end if e.start == u else e.start
                        if v not in comp:
                            comp[v] = label
                            stack.append(v)
                label += 1
            self._components = comp
        return self._components

    def nearest_node(self, point_proj: Point, component: int | None = None) -> NodeKey:
        comps = self.components() if component is not None else None
        best: NodeKey | None = None
        best_d = math.inf
        for k, (x, y) in self.positions.items():
            if comps is not None and comps[k] != component:
                continue
            d = math.hypot(x - point_proj.x, y - point_proj.y)
            if d < best_d:
                best, best_d = k, d
        if best is None:
            raise AoiBuildError("river network is empty")
        return best

    def shortest_path(self, start: NodeKey, end: NodeKey) -> list[Edge] | None:
        """Dijkstra over edge lengths; returns the edge sequence oriented start→end."""
        dist: dict[NodeKey, float] = {start: 0.0}
        prev: dict[NodeKey, tuple[NodeKey, int]] = {}
        heap: list[tuple[float, NodeKey]] = [(0.0, start)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            if u == end:
                break
            for idx in self.adjacency[u]:
                e = self.edges[idx]
                v = e.end if e.start == u else e.start
                nd = d + e.length_m
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, idx)
                    heapq.heappush(heap, (nd, v))
        if end not in dist:
            return None
        path: list[Edge] = []
        node = end
        while node != start:
            u, idx = prev[node]
            e = self.edges[idx]
            if e.start != u:
                e = Edge(e.way_id, e.end, e.start, e.length_m, tuple(reversed(e.coords_proj)))
            path.append(e)
            node = u
        path.reverse()
        return path


@dataclass(frozen=True)
class CentrelineResult:
    line_proj: Any
    """Oriented, clipped centreline in the AOI's projected CRS."""

    start_key: NodeKey
    end_key: NodeKey
    start_offset_m: float
    """Distance from the source-zone centroid to the first network node."""

    full_length_m: float
    clipped: bool
    way_ids: tuple[int, ...]


def build_centreline(
    network: RiverNetwork,
    source_zone_proj: Polygon,
    target_proj: Point,
    chainage_m: float,
) -> CentrelineResult:
    """Shortest path from the node nearest the source zone to the node nearest the target."""
    if not network.positions:
        raise AoiBuildError("no waterway ways in the Overpass response")
    end = network.nearest_node(target_proj)
    component = network.components()[end]
    centroid = source_zone_proj.centroid
    start = network.nearest_node(centroid, component=component)
    if start == end:
        raise AoiBuildError("source zone and downstream target resolve to the same node")
    path = network.shortest_path(start, end)
    if path is None:  # pragma: no cover - same component by construction
        raise AoiBuildError("no path between source zone and downstream target")
    coords: list[tuple[float, float]] = []
    for e in path:
        for p in e.coords_proj:
            if not coords or p != coords[-1]:
                coords.append(p)
    line: Any = LineString(coords)
    full_length = float(line.length)
    clipped = full_length > chainage_m
    if clipped:
        line = substring(line, 0.0, chainage_m)
    sx, sy = network.positions[start]
    return CentrelineResult(
        line_proj=line,
        start_key=start,
        end_key=end,
        start_offset_m=float(math.hypot(sx - centroid.x, sy - centroid.y)),
        full_length_m=full_length,
        clipped=clipped,
        way_ids=tuple(dict.fromkeys(e.way_id for e in path)),
    )


# --- grid and extents -------------------------------------------------------------------------


def transformers(epsg: int) -> tuple[Transformer, Transformer]:
    to_proj = Transformer.from_crs(4326, epsg, always_xy=True)
    to_wgs = Transformer.from_crs(epsg, 4326, always_xy=True)
    return to_proj, to_wgs


def bbox_polygon(bbox: Bbox4326) -> Polygon:
    w, s, e, n = bbox
    return box(w, s, e, n)


def _project(geom: Any, tr: Transformer) -> Any:
    return transform(tr.transform, geom)


def bbox_4326_of(geom_proj: Any, to_wgs: Transformer, densify_m: float = 500.0) -> Bbox4326:
    """WGS 84 bounds of a projected geometry, with edges densified before reprojection."""
    dense = segmentize(geom_proj, densify_m)
    minx, miny, maxx, maxy = _project(dense, to_wgs).bounds
    return (
        round(float(minx), COORD_DECIMALS),
        round(float(miny), COORD_DECIMALS),
        round(float(maxx), COORD_DECIMALS),
        round(float(maxy), COORD_DECIMALS),
    )


def grid_from_bbox(
    aoi_id: str, epsg: int, bbox: Bbox4326, resolution_m: float = DEFAULT_RESOLUTION_M
) -> GridSpec:
    """The 30 m grid covering `bbox`: project its densified outline, snap outward."""
    to_proj, _ = transformers(epsg)
    outline = segmentize(bbox_polygon(bbox), 0.005)
    minx, miny, maxx, maxy = _project(outline, to_proj).bounds
    x_min = math.floor(minx / resolution_m) * resolution_m
    y_min = math.floor(miny / resolution_m) * resolution_m
    width = math.ceil((maxx - x_min) / resolution_m)
    height = math.ceil((maxy - y_min) / resolution_m)
    return GridSpec(
        aoi_id=aoi_id,
        epsg=epsg,
        resolution_m=resolution_m,
        x_min=x_min,
        y_min=y_min,
        x_max=x_min + width * resolution_m,
        y_max=y_min + height * resolution_m,
        width=width,
        height=height,
    )


# --- specs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransectSpec:
    id: str
    name: str
    osm_node_id: int | None = None
    osm_way_id: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class StatedLocation:
    """A location stated by a retrieved source (never invented); flagged approximate."""

    lon: float
    lat: float
    positional_accuracy_m: float
    basis: str


@dataclass(frozen=True)
class AssetSpec:
    id: str
    name: str
    asset_type: AssetType
    status: AssetStatus
    source_refs: tuple[str, ...]
    osm_node_id: int | None = None
    osm_way_id: int | None = None
    stated_location: StatedLocation | None = None
    capacity_mw: Range | None = None
    population: Range | None = None
    notes: str | None = None
    positional_accuracy_m: float | None = None


@dataclass(frozen=True)
class AoiSpec:
    id: str
    name: str
    countries: tuple[str, ...]
    epsg: int
    source_zone_bbox: Bbox4326
    river_names: tuple[str, ...]
    overpass_query: str
    downstream_target: LonLat
    chainage_km: float
    fixture_path: str
    """Repo-relative path of the committed Overpass response."""

    fixture_retrieved_utc: datetime
    transects: tuple[TransectSpec, ...]
    assets: tuple[AssetSpec, ...]
    sources: tuple[SourceRef, ...]
    """Every non-OSM source; the OSM `dataset` SourceRef is generated at build time."""

    extent_source_refs: tuple[str, ...]
    """Ids of the sources backing the source-zone / extent design choices."""

    notes: str
    record_created_utc: datetime
    corridor_buffer_m: float = 1500.0
    extent_buffer_m: float = 3000.0
    resolution_m: float = DEFAULT_RESOLUTION_M
    waterway_kinds: frozenset[str] = WATERWAY_KINDS
    transect_link_max_m: float = 5000.0
    """An asset gets a `transect_id` only if it lies within this distance of the centreline."""

    @property
    def osm_source_id(self) -> str:
        return f"osm-overpass-{self.id}"


# --- build ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildReport:
    osm_timestamp: str | None
    osm_generator: str | None
    response_sha256: str
    response_bytes: int
    way_count: int
    node_count: int
    centreline_length_km: float
    full_path_length_km: float
    clipped: bool
    start_offset_m: float
    start_lonlat: LonLat
    end_lonlat: LonLat
    transect_chainage_km: Mapping[str, float]
    transect_offset_m: Mapping[str, float]
    way_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "osm_timestamp": self.osm_timestamp,
            "osm_generator": self.osm_generator,
            "response_sha256": self.response_sha256,
            "response_bytes": self.response_bytes,
            "way_count": self.way_count,
            "node_count": self.node_count,
            "centreline_length_km": self.centreline_length_km,
            "full_path_length_km": self.full_path_length_km,
            "clipped": self.clipped,
            "start_offset_m": self.start_offset_m,
            "start_lonlat": list(self.start_lonlat),
            "end_lonlat": list(self.end_lonlat),
            "transect_chainage_km": dict(self.transect_chainage_km),
            "transect_offset_m": dict(self.transect_offset_m),
            "way_ids": list(self.way_ids),
        }


@dataclass
class BuiltAoi:
    spec: AoiSpec
    aoi: AOI
    source_zone: Any
    centreline: Any
    corridor: Any
    transects: list[Transect]
    assets: list[ExposedAsset]
    asset_accuracy_m: Mapping[str, float]
    report: BuildReport
    raw_response: bytes = field(repr=False)


def osm_source_ref(
    spec: AoiSpec, raw: bytes, data: OverpassData, accessed_utc: datetime
) -> SourceRef:
    claims = ["river_centreline", "corridor", "cube_extent_bbox_4326", "river_names"]
    claims += [f"transects.{t.id}.point" for t in spec.transects]
    claims += [
        f"exposed_assets.{a.id}.geometry"
        for a in spec.assets
        if a.osm_node_id is not None or a.osm_way_id is not None
    ]
    return SourceRef(
        id=spec.osm_source_id,
        kind=SourceKind.dataset,
        title=(
            "OpenStreetMap hydrography, places and infrastructure via the Overpass API "
            f"(osm_base {data.timestamp_osm_base}; query recorded in notes)"
        ),
        url=OVERPASS_ENDPOINT,
        authors=["OpenStreetMap contributors"],
        year=accessed_utc.year,
        publisher="OpenStreetMap Foundation",
        accessed_utc=accessed_utc,
        sha256=hashlib.sha256(raw).hexdigest(),
        content_type="application/json",
        licence=OSM_LICENCE,
        stored_copy=spec.fixture_path,
        claims_supported=claims,
        excerpt=data.copyright,
        peer_reviewed=False,
    )


def _lonlat_of(data: OverpassData, node_id: int | None, way_id: int | None) -> LonLat:
    if node_id is not None:
        n = data.node(node_id)
        return (n.lon, n.lat)
    if way_id is not None:
        return data.way(way_id).centroid()
    raise AoiBuildError("neither osm_node_id nor osm_way_id given")


def _round_pos(lon: float, lat: float) -> tuple[float, float]:
    return (round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS))


def build_aoi(
    spec: AoiSpec,
    client: OverpassClient,
    *,
    accessed_utc: datetime | None = None,
    created_by: str = ADAPTER_NAME,
) -> BuiltAoi:
    """Run the whole pipeline for one AOI and return the models plus a build report."""
    raw = client.query(spec.overpass_query)
    data = parse_overpass(raw)
    accessed = accessed_utc or spec.fixture_retrieved_utc
    to_proj, to_wgs = transformers(spec.epsg)

    source_zone = bbox_polygon(spec.source_zone_bbox)
    source_zone_proj = _project(source_zone, to_proj)
    target_proj = Point(*to_proj.transform(*spec.downstream_target))
    network = RiverNetwork(data.waterway_ways(spec.waterway_kinds), to_proj)
    centre = build_centreline(network, source_zone_proj, target_proj, spec.chainage_km * 1000.0)
    line_proj = centre.line_proj
    corridor_proj = line_proj.buffer(spec.corridor_buffer_m)
    extent_proj = unary_union([source_zone_proj, corridor_proj]).buffer(spec.extent_buffer_m)
    extent_bbox = bbox_4326_of(extent_proj, to_wgs)
    grid = grid_from_bbox(spec.id, spec.epsg, extent_bbox, spec.resolution_m)

    osm_ref = osm_source_ref(spec, raw, data, accessed)
    sources = [osm_ref, *spec.sources]
    source_ids = {s.id for s in sources}

    transects: list[Transect] = []
    chainages: dict[str, float] = {}
    offsets: dict[str, float] = {}
    for t in spec.transects:
        lon, lat = _lonlat_of(data, t.osm_node_id, t.osm_way_id)
        p = Point(*to_proj.transform(lon, lat))
        d_along = float(line_proj.project(p))
        snapped = line_proj.interpolate(d_along)
        offset = float(p.distance(snapped))
        if centre.clipped and d_along >= line_proj.length - 1e-6:
            raise AoiBuildError(
                f"transect {t.id!r} projects at or beyond the clipped centreline end; "
                f"increase chainage_km for {spec.id}"
            )
        slon, slat = to_wgs.transform(snapped.x, snapped.y)
        chainage_km = round(d_along / 1000.0, 3)
        chainages[t.id] = chainage_km
        offsets[t.id] = round(offset, 1)
        basis = (
            f"OSM node {t.osm_node_id}" if t.osm_node_id is not None else f"OSM way {t.osm_way_id}"
        )
        note = (
            f"Snapped from {basis} to the OSM centreline; the feature lies "
            f"{offset:.0f} m from the snapped point."
        )
        if t.notes:
            note = f"{note} {t.notes}"
        transects.append(
            Transect(
                id=t.id,
                aoi_id=spec.id,
                name=t.name,
                chainage_km=chainage_km,
                point=GeoPoint(coordinates=_round_pos(slon, slat)),
                geometry_quality=GeometryQuality.snapped_to_osm_centreline,
                positional_accuracy_m=round(max(50.0, offset), 1),
                source_refs=[osm_ref.id],
                notes=note,
            )
        )

    assets: list[ExposedAsset] = []
    accuracies: dict[str, float] = {}
    for a in spec.assets:
        missing = [r for r in a.source_refs if r not in source_ids]
        if missing:
            raise AoiBuildError(f"asset {a.id!r} cites unknown sources {missing}")
        if a.stated_location is not None:
            lon, lat = a.stated_location.lon, a.stated_location.lat
            quality = GeometryQuality.source_stated_location
            accuracy = a.stated_location.positional_accuracy_m
            basis = a.stated_location.basis
        else:
            lon, lat = _lonlat_of(data, a.osm_node_id, a.osm_way_id)
            quality = (
                GeometryQuality.osm_node
                if a.osm_node_id is not None
                else GeometryQuality.osm_way_centroid
            )
            accuracy = a.positional_accuracy_m if a.positional_accuracy_m is not None else 50.0
            basis = (
                f"OSM node {a.osm_node_id}"
                if a.osm_node_id is not None
                else f"centroid of OSM way {a.osm_way_id}"
            )
        p = Point(*to_proj.transform(lon, lat))
        d_along = float(line_proj.project(p))
        dist = float(p.distance(line_proj))
        transect_id: str | None = None
        if transects and dist <= spec.transect_link_max_m:
            transect_id = min(transects, key=lambda t: abs(t.chainage_km * 1000.0 - d_along)).id
        note = f"Location: {basis}; {dist / 1000.0:.1f} km from the centreline."
        if a.notes:
            note = f"{note} {a.notes}"
        refs = list(a.source_refs)
        if a.stated_location is None and osm_ref.id not in refs:
            refs.append(osm_ref.id)
        assets.append(
            ExposedAsset(
                id=a.id,
                aoi_id=spec.id,
                name=a.name,
                asset_type=a.asset_type,
                status=a.status,
                geometry=GeoPoint(coordinates=_round_pos(lon, lat)),
                capacity_mw=a.capacity_mw,
                population=a.population,
                transect_id=transect_id,
                geometry_quality=quality,
                source_refs=refs,
                notes=note,
            )
        )
        accuracies[a.id] = accuracy

    centre_wgs = _project(line_proj, to_wgs)
    corridor_wgs = orient(_project(corridor_proj, to_wgs), 1.0)
    sx, sy = network.positions[centre.start_key]
    ex, ey = line_proj.coords[-1]
    report = BuildReport(
        osm_timestamp=data.timestamp_osm_base,
        osm_generator=data.generator,
        response_sha256=osm_ref.sha256,
        response_bytes=len(raw),
        way_count=len(data.ways),
        node_count=len(data.nodes),
        centreline_length_km=round(float(line_proj.length) / 1000.0, 3),
        full_path_length_km=round(centre.full_length_m / 1000.0, 3),
        clipped=centre.clipped,
        start_offset_m=round(centre.start_offset_m, 1),
        start_lonlat=_round_pos(*to_wgs.transform(sx, sy)),
        end_lonlat=_round_pos(*to_wgs.transform(ex, ey)),
        transect_chainage_km=chainages,
        transect_offset_m=offsets,
        way_ids=centre.way_ids,
    )
    aoi = AOI(
        id=spec.id,
        name=spec.name,
        countries=list(spec.countries),
        cube_epsg=spec.epsg,
        cube_extent_bbox_4326=extent_bbox,
        grid=grid,
        river_names=list(spec.river_names),
        source_refs=[osm_ref.id, *spec.extent_source_refs],
        sources=sources,
        notes=_compose_notes(spec, report),
        record=RecordMeta(created_utc=spec.record_created_utc, created_by=created_by),
    )
    return BuiltAoi(
        spec=spec,
        aoi=aoi,
        source_zone=source_zone,
        centreline=centre_wgs,
        corridor=corridor_wgs,
        transects=transects,
        assets=assets,
        asset_accuracy_m=accuracies,
        report=report,
        raw_response=raw,
    )


def _compose_notes(spec: AoiSpec, report: BuildReport) -> str:
    chain = ", ".join(f"{k}={v:.1f} km" for k, v in report.transect_chainage_km.items())
    clipped = (
        f"clipped at {spec.chainage_km:.0f} km of a {report.full_path_length_km:.1f} km path"
        if report.clipped
        else f"not clipped ({report.centreline_length_km:.1f} km available)"
    )
    build = (
        f"BUILD (from OSM data as of {report.osm_timestamp}, {OSM_ATTRIBUTION}, {OSM_LICENCE}): "
        f"centreline chainage 0 is the OSM river-network node nearest the source-zone centroid "
        f"that connects to the downstream target, {report.start_offset_m / 1000.0:.2f} km from "
        f"the centroid at {report.start_lonlat}; {clipped}; transect chainages: {chain}. "
        f"Corridor = centreline buffered {spec.corridor_buffer_m:.0f} m in EPSG:{spec.epsg}; cube "
        f"extent = source zone union corridor buffered {spec.extent_buffer_m:.0f} m, "
        f"{spec.resolution_m:.0f} m grid snapped to the resolution. "
        f"OVERPASS QUERY: {' '.join(spec.overpass_query.split())}"
    )
    return f"{spec.notes.strip()} {build}"


# --- writing ----------------------------------------------------------------------------------


def _geom_dict(geom: Any) -> dict[str, Any]:
    d = mapping(geom)
    out: dict[str, Any] = json.loads(
        json.dumps(d), parse_float=lambda s: round(float(s), COORD_DECIMALS)
    )
    return out


def _base_props(
    ftype: FeatureType, source_refs: Sequence[str], quality: GeometryQuality, accuracy: float
) -> dict[str, Any]:
    return {
        "feature_type": ftype.value,
        "source_refs": list(source_refs),
        "geometry_quality": quality.value,
        "positional_accuracy_m": accuracy,
    }


def write_aoi_dir(built: BuiltAoi, out_dir: Path) -> list[Path]:
    """Write the seven AOI files; returns the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = built.spec
    osm_id = spec.osm_source_id
    written: list[Path] = []

    def put(name: str, payload: dict[str, Any]) -> None:
        path = out_dir / name
        dump_geojson(path, payload) if name.endswith(".geojson") else path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        written.append(path)

    put("aoi.json", json.loads(built.aoi.model_dump_json(exclude_none=True)))
    put("grid.json", json.loads(built.aoi.grid.model_dump_json()) if built.aoi.grid else {})

    sz_props = _base_props(
        FeatureType.source_zone,
        [osm_id, *spec.extent_source_refs],
        GeometryQuality.hand_digitised_approximate,
        1000.0,
    )
    sz_props.update(
        {
            "aoi_id": spec.id,
            "bbox_4326": list(spec.source_zone_bbox),
            "notes": "Design rectangle (see aoi.json notes); not a mapped scar or deposit.",
        }
    )
    put(
        "source_zone.geojson",
        feature_collection([feature(_geom_dict(orient(built.source_zone, 1.0)), sz_props)]),
    )

    cl_props = _base_props(
        FeatureType.river_centreline, [osm_id], GeometryQuality.osm_centreline, 30.0
    )
    cl_props.update(
        {
            "aoi_id": spec.id,
            "epsg_for_chainage": spec.epsg,
            "chainage_km": {"start": 0.0, "end": built.report.centreline_length_km},
            "osm_way_ids": list(built.report.way_ids),
            "osm_timestamp": built.report.osm_timestamp,
            "attribution": OSM_ATTRIBUTION,
            "licence": OSM_LICENCE,
        }
    )
    put(
        "river_centreline.geojson",
        feature_collection([feature(_geom_dict(built.centreline), cl_props)]),
    )

    co_props = _base_props(FeatureType.corridor, [osm_id], GeometryQuality.osm_centreline, 30.0)
    co_props.update(
        {"aoi_id": spec.id, "buffer_m": spec.corridor_buffer_m, "buffer_epsg": spec.epsg}
    )
    put("corridor.geojson", feature_collection([feature(_geom_dict(built.corridor), co_props)]))

    tr_features = []
    for t in built.transects:
        props = json.loads(t.model_dump_json(exclude={"point"}, exclude_none=True))
        props["feature_type"] = FeatureType.transect.value
        tr_features.append(feature(json.loads(t.point.model_dump_json()), props))
    put("transects.geojson", feature_collection(tr_features))

    as_features = []
    for a in built.assets:
        props = json.loads(a.model_dump_json(exclude={"geometry"}, exclude_none=True))
        props["feature_type"] = FeatureType.exposed_asset.value
        props["positional_accuracy_m"] = built.asset_accuracy_m.get(a.id, 50.0)
        as_features.append(
            feature(json.loads(a.model_dump_json(include={"geometry"}))["geometry"], props)
        )
    put("exposed_assets.geojson", feature_collection(as_features))
    return written


# --- reading ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AoiFiles:
    """Parsed contents of one AOI directory (models where a model exists)."""

    path: Path
    aoi: AOI
    grid: GridSpec
    source_zone: dict[str, Any]
    centreline: dict[str, Any]
    corridor: dict[str, Any]
    transects: list[Transect]
    assets: list[ExposedAsset]
    raw: Mapping[str, dict[str, Any]]


def _load_json(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc


def features_of(collection: Mapping[str, Any]) -> list[dict[str, Any]]:
    feats = collection.get("features")
    if not isinstance(feats, list):
        raise ValueError("not a FeatureCollection")
    return [f for f in feats if isinstance(f, dict)]


def transect_from_feature(f: Mapping[str, Any]) -> Transect:
    props = {k: v for k, v in f["properties"].items() if k != "feature_type"}
    return Transect.model_validate({**props, "point": f["geometry"]})


def asset_from_feature(f: Mapping[str, Any]) -> ExposedAsset:
    props = {
        k: v
        for k, v in f["properties"].items()
        if k not in ("feature_type", "positional_accuracy_m")
    }
    return ExposedAsset.model_validate({**props, "geometry": f["geometry"]})


def read_aoi_dir(path: Path) -> AoiFiles:
    raw = {name: _load_json(path / name) for name in AOI_FILES}
    aoi = AOI.model_validate(raw["aoi.json"])
    grid = GridSpec.model_validate(raw["grid.json"])
    return AoiFiles(
        path=path,
        aoi=aoi,
        grid=grid,
        source_zone=raw["source_zone.geojson"],
        centreline=raw["river_centreline.geojson"],
        corridor=raw["corridor.geojson"],
        transects=[transect_from_feature(f) for f in features_of(raw["transects.geojson"])],
        assets=[asset_from_feature(f) for f in features_of(raw["exposed_assets.geojson"])],
        raw=raw,
    )


def iter_aoi_dirs(repo: Path) -> Iterator[Path]:
    root = repo / "data" / "aoi"
    if not root.exists():
        return
    for p in sorted(root.iterdir()):
        if p.is_dir() and (p / "aoi.json").exists():
            yield p


def geometry_to_shapely(geom: Geometry | Mapping[str, Any]) -> Any:
    """GeoJSON geometry (model or dict) → shapely geometry."""
    from shapely.geometry import shape

    doc: dict[str, Any] = (
        dict(geom) if isinstance(geom, Mapping) else json.loads(geom.model_dump_json())
    )
    return shape(doc)


def project_to[G: BaseGeometry](geom: G, epsg: int) -> G:
    """Reproject a lon/lat geometry to `epsg` (metres); the only shapely/pyproj hop callers need."""
    from shapely.ops import transform as sh_transform

    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    out: G = sh_transform(tr.transform, geom)
    return out
