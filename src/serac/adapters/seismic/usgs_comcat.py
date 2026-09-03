"""USGS ComCat event-catalogue adapter (`EventCatalog`) over httpx.

`eventtype=landslide` is the labelled positive set for Prompt 2. It is small: the committed
fixture (`data/fixtures/usgs_comcat/landslide_2000-01-01_2026-09-03.geojson`) holds 57 events
since 2000, mostly Alaska ml 1-2, and Chamoli 2021 is **not** among them (RELEASE_STATUS.md
Known gaps 8). ComCat is not case-consistent about `type` (`landslide` and `Landslide` both
occur); this adapter lower-cases it.

Pagination follows the fdsnws-event convention: `limit` per page and a 1-based `offset`; a
page shorter than `limit` ends the walk. `fetch()` writes each page's bytes verbatim under
`data/raw/usgs_comcat/` and appends a ledger row per page (US public domain).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.errors import SeracError
from serac.ports.ledger import ManifestLedger
from serac.ports.seismic import CatalogEvent, CatalogQuery, EventCatalog

ADAPTER_NAME = "ComCatCatalog"
ADAPTER_VERSION = "0.1.0"

COMCAT_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
COMCAT_MAX_LIMIT = 20_000
LICENCE = "US-PD"
LICENCE_SOURCE_URL = (
    "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits"
)
LICENCE_NOTE = (
    "USGS-authored or produced data and information are considered to be in the U.S. Public Domain."
)
# From the committed fixture: 41870 bytes for 57 features.
FIXTURE_BYTES_PER_FEATURE = 41870 / 57


class ComCatError(SeracError):
    """ComCat could not be queried or its response could not be parsed."""


def _fdsn_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _ms_to_utc(value: Any) -> datetime:
    if not isinstance(value, int | float):
        raise ComCatError(f"feature time is not epoch milliseconds: {value!r}")
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ComCatError(f"expected a number, got {value!r}")
    return float(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_feature(feature: dict[str, Any]) -> CatalogEvent:
    """Flatten one ComCat geojson feature; `type` is lower-cased."""
    props = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        raise ComCatError("feature lacks properties/geometry")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ComCatError("feature geometry lacks coordinates")
    raw_type = props.get("type")
    event_type = raw_type.lower() if isinstance(raw_type, str) else None
    depth = _optional_float(coords[2]) if len(coords) > 2 else None
    return CatalogEvent(
        event_id=str(feature.get("id") or props.get("code") or ""),
        time_utc=_ms_to_utc(props.get("time")),
        latitude=float(coords[1]),
        longitude=float(coords[0]),
        depth_km=depth,
        magnitude=_optional_float(props.get("mag")),
        mag_type=_optional_str(props.get("magType")),
        event_type=event_type,
        title=_optional_str(props.get("title")),
        url=_optional_str(props.get("url")),
        source_agency=_optional_str(props.get("net")),
        raw=dict(props),
    )


def parse_geojson(doc: dict[str, Any]) -> list[CatalogEvent]:
    """Events from a ComCat `FeatureCollection` (or a single `Feature`)."""
    kind = doc.get("type")
    if kind == "Feature":
        return [parse_feature(doc)]
    if kind != "FeatureCollection":
        raise ComCatError(f"not a geojson FeatureCollection: type={kind!r}")
    features = doc.get("features")
    if not isinstance(features, list):
        raise ComCatError("FeatureCollection has no features list")
    return [parse_feature(f) for f in features]


def load_fixture(path: Path) -> list[CatalogEvent]:
    """Parse a committed ComCat geojson file."""
    return parse_geojson(json.loads(path.read_text(encoding="utf-8")))


def _matches(event: CatalogEvent, query: CatalogQuery) -> bool:
    if not (query.start_utc <= event.time_utc < query.end_utc):
        return False
    if query.event_type is not None and event.event_type != query.event_type.lower():
        return False
    if query.min_magnitude is not None and (
        event.magnitude is None or event.magnitude < query.min_magnitude
    ):
        return False
    if query.bbox_4326 is not None:
        w, s, e, n = query.bbox_4326
        if not (w <= event.longitude <= e and s <= event.latitude <= n):
            return False
    return not (query.event_id is not None and event.event_id != query.event_id)


def filter_events(events: list[CatalogEvent], query: CatalogQuery) -> list[CatalogEvent]:
    """Apply a `CatalogQuery` to already-parsed events (used for fixture-backed queries)."""
    return [e for e in events if _matches(e, query)][: query.limit]


def query_params(query: CatalogQuery, *, limit: int, offset: int) -> dict[str, str]:
    """fdsnws-event query string for one page."""
    params: dict[str, str] = {
        "format": "geojson",
        "starttime": _fdsn_time(query.start_utc),
        "endtime": _fdsn_time(query.end_utc),
        "orderby": "time-asc",
        "limit": str(limit),
        "offset": str(offset),
    }
    if query.event_type is not None:
        params["eventtype"] = query.event_type
    if query.min_magnitude is not None:
        params["minmagnitude"] = str(query.min_magnitude)
    if query.bbox_4326 is not None:
        w, s, e, n = query.bbox_4326
        params.update(
            {
                "minlongitude": str(w),
                "maxlongitude": str(e),
                "minlatitude": str(s),
                "maxlatitude": str(n),
            }
        )
    if query.event_id is not None:
        params["eventid"] = query.event_id
    return params


def page_name(query: CatalogQuery, *, page: int) -> str:
    """Deterministic file stem for a page of results."""
    kind = (query.event_type or "any").lower()
    stem = f"{kind}_{query.start_utc:%Y-%m-%d}_{query.end_utc:%Y-%m-%d}"
    if query.event_id:
        stem = query.event_id
    return stem if page == 1 else f"{stem}.p{page}"


class ComCatPlan:
    """Dry-run description: the page-1 URL and a stated byte estimate; nothing is fetched."""

    def __init__(self, query: CatalogQuery, url: str, params: dict[str, str]) -> None:
        self.query = query
        self.url = url
        self.params = params

    @property
    def estimate_basis(self) -> str:
        return (
            "event count unknown before the request; the committed fixture measured "
            f"{FIXTURE_BYTES_PER_FEATURE:.0f} bytes per feature (41870 B / 57 events)"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": ADAPTER_NAME,
            "url": str(httpx.URL(self.url, params=self.params)),
            "params": dict(self.params),
            "estimated_bytes": None,
            "estimate_basis": self.estimate_basis,
            "licence": LICENCE,
            "licence_source_url": LICENCE_SOURCE_URL,
        }


class ComCatCatalog(EventCatalog):
    """`EventCatalog` over ComCat's fdsnws-event service, paginated by offset."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str = COMCAT_QUERY_URL,
        page_size: int = COMCAT_MAX_LIMIT,
        timeout: float = 60.0,
        repo_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= page_size <= COMCAT_MAX_LIMIT:
            raise ComCatError(f"page_size must be within 1..{COMCAT_MAX_LIMIT}")
        self._client = client
        self.base_url = base_url
        self.page_size = page_size
        self.timeout = timeout
        self.repo_root = repo_root
        self._now = clock or (lambda: datetime.now(tz=UTC))

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def plan(self, query: CatalogQuery) -> ComCatPlan:
        return self._plan(query)

    def _plan(self, query: CatalogQuery) -> ComCatPlan:
        limit = min(self.page_size, query.limit)
        return ComCatPlan(query, self.base_url, query_params(query, limit=limit, offset=1))

    def _get_page(
        self, query: CatalogQuery, *, limit: int, offset: int
    ) -> tuple[bytes, dict[str, Any]]:
        params = query_params(query, limit=limit, offset=offset)
        try:
            response = self.client.get(self.base_url, params=params)
        except httpx.HTTPError as exc:
            raise ComCatError(f"ComCat request failed: {exc}") from exc
        if response.status_code == 204:
            return response.content, {"type": "FeatureCollection", "features": []}
        if response.status_code != 200:
            raise ComCatError(f"ComCat returned HTTP {response.status_code}: {response.text[:200]}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise ComCatError(f"ComCat response is not JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise ComCatError("ComCat response is not a JSON object")
        return response.content, doc

    def _pages(self, query: CatalogQuery) -> list[tuple[int, bytes, list[CatalogEvent]]]:
        """Walk offset pages until a short page or the query limit; returns (page, raw, events)."""
        pages: list[tuple[int, bytes, list[CatalogEvent]]] = []
        offset = 1
        remaining = query.limit
        page = 1
        while remaining > 0:
            limit = min(self.page_size, remaining)
            raw, doc = self._get_page(query, limit=limit, offset=offset)
            events = parse_geojson(doc)
            pages.append((page, raw, events))
            if len(events) < limit:
                break
            remaining -= len(events)
            offset += len(events)
            page += 1
        return pages

    def query(self, query: CatalogQuery) -> list[CatalogEvent]:
        events = [e for _, _, page in self._pages(query) for e in page]
        return events[: query.limit]

    def fetch(self, query: CatalogQuery, dest_root: Path, ledger: ManifestLedger) -> list[Path]:
        """Query, write every page verbatim under `dest_root/usgs_comcat/`, ledger each page."""
        dest = dest_root / "usgs_comcat"
        dest.mkdir(parents=True, exist_ok=True)
        retrieved_at = self._now()
        written: list[Path] = []
        for page, raw, events in self._pages(query):
            path = dest / f"{page_name(query, page=page)}.geojson"
            path.write_bytes(raw)
            written.append(path)
            limit = min(self.page_size, query.limit)
            offset = 1 + (page - 1) * self.page_size
            params = query_params(query, limit=limit, offset=offset)
            ledger.append(
                ManifestEntry(
                    source=DataSource.usgs_comcat,
                    product_id=page_name(query, page=page),
                    path=self._rel(path),
                    url=str(httpx.URL(self.base_url, params=params)),
                    params={k: v for k, v in params.items()},
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                    retrieved_at=retrieved_at,
                    licence=LICENCE,
                    licence_source_url=LICENCE_SOURCE_URL,
                    provenance=Provenance.real,
                    status=ManifestStatus.fetched,
                    time_start=query.start_utc,
                    time_end=query.end_utc,
                    bbox_4326=query.bbox_4326,
                    adapter=ADAPTER_NAME,
                    adapter_version=ADAPTER_VERSION,
                    notes=f"{LICENCE_NOTE} {len(events)} feature(s) on this page.",
                )
            )
        return written

    def _rel(self, path: Path) -> str:
        if self.repo_root is not None:
            try:
                return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
