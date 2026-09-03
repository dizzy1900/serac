"""ASF plumbing shared by the Sentinel-1 and NISAR adapters.

`asf_search` is wrapped behind two small Protocols so the adapters are tested against the
committed listings without importing the library at test time:

* `AsfSearchClient.geo_search(...)` returns plain GeoJSON feature dicts (what
  `ASFSearchResults.geojson()["features"]` yields, and what the fixtures store);
* `EarthdataDownloader.download(url, dest)` streams one granule with an Earthdata Login
  session and returns (sha256, size).

Search is public; downloads need `EARTHDATA_USERNAME/PASSWORD` (docs/CREDENTIALS.md).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from serac.ports.ingest import Bbox4326, CredentialSpec

ASF_SEARCH_URL = "https://api.daac.asf.alaska.edu/services/search/param"
NASA_DATA_POLICY_URL = (
    "https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/"
    "data-information-guidance"
)
CHUNK_BYTES = 1 << 20

EARTHDATA_CREDENTIAL = CredentialSpec(
    name="Earthdata Login",
    env_vars=("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"),
    purpose="download granules from ASF / NASA Earthdata Cloud (search is public)",
)


class AsfSearchClient(Protocol):
    """The one search call the adapters make; fakes serve recorded feature lists."""

    def geo_search(
        self,
        *,
        intersects_with: str,
        platform: Sequence[str],
        start: datetime | None,
        end: datetime | None,
        processing_level: Sequence[str] | None,
        beam_mode: Sequence[str] | None,
        flight_direction: str | None,
        relative_orbit: Sequence[int] | None,
        max_results: int | None,
    ) -> list[dict[str, Any]]: ...


class AsfSearchLibClient:
    """`AsfSearchClient` over `asf_search.geo_search`; the production choice."""

    def geo_search(
        self,
        *,
        intersects_with: str,
        platform: Sequence[str],
        start: datetime | None,
        end: datetime | None,
        processing_level: Sequence[str] | None,
        beam_mode: Sequence[str] | None,
        flight_direction: str | None,
        relative_orbit: Sequence[int] | None,
        max_results: int | None,
    ) -> list[dict[str, Any]]:
        import asf_search

        kwargs: dict[str, Any] = {
            "intersectsWith": intersects_with,
            "platform": list(platform),
        }
        if start is not None:
            kwargs["start"] = start
        if end is not None:
            kwargs["end"] = end
        if processing_level:
            kwargs["processingLevel"] = list(processing_level)
        if beam_mode:
            kwargs["beamMode"] = list(beam_mode)
        if flight_direction:
            kwargs["flightDirection"] = flight_direction
        if relative_orbit:
            kwargs["relativeOrbit"] = list(relative_orbit)
        if max_results is not None:
            kwargs["maxResults"] = max_results
        results = asf_search.geo_search(**kwargs)
        features: list[dict[str, Any]] = list(results.geojson()["features"])
        return features


class EarthdataDownloader(Protocol):
    """Streams one Earthdata-authenticated URL to disk; returns (sha256 hex, size)."""

    def download(self, url: str, dest: Path) -> tuple[str, int]: ...


class AsfSessionDownloader:
    """`EarthdataDownloader` over `asf_search.ASFSession().auth_with_creds`."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session: Any = None

    def _open(self) -> Any:
        if self._session is None:
            import asf_search

            self._session = asf_search.ASFSession().auth_with_creds(self._username, self._password)
        return self._session

    def download(self, url: str, dest: Path) -> tuple[str, int]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with self._open().get(url, stream=True) as response, part.open("wb") as fh:
                response.raise_for_status()
                for chunk in response.iter_content(CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    fh.write(chunk)
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), size


def bbox_wkt(bbox: Bbox4326) -> str:
    """WKT polygon for `intersectsWith`."""
    w, s, e, n = bbox
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def parse_asf_time(text: str | None) -> datetime | None:
    """ASF times are ISO-8601 with a `Z` suffix."""
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def feature_bbox(feature: dict[str, Any]) -> Bbox4326 | None:
    """Envelope of a GeoJSON Polygon/MultiPolygon footprint, or None."""
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates")
    if not coords:
        return None
    points: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            points.append((float(node[0]), float(node[1])))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))
