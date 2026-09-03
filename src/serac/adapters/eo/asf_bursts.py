"""ASF `SLC-BURST` search: the granule listing the watch layer plans its network from.

Sentinel-1 burst granules are the input vocabulary of HyP3's burst InSAR jobs, and the search
is public — no credential is needed to discover them, only to process them. As elsewhere in
serac the library call sits behind a Protocol so tests read a committed listing instead.

Listings are cached under `data/interim/watch/bursts/<aoi>_<start>_<end>.json` and ledgered,
because a five-year listing is several megabytes of JSON that the planner, the submitter and
the poller all re-read, and because the network plan is only reproducible if the listing it
was derived from is on disk with a checksum.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

ASF_SEARCH_DOCS: Final[str] = "https://docs.asf.alaska.edu/asf_search/basics/"
BURST_LICENCE: Final[str] = (
    "Copernicus Sentinel data, free and open under the Sentinel Data Legal Notice; burst "
    "extraction and hosting by ASF DAAC"
)
BURST_LICENCE_URL: Final[str] = (
    "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice"
)
BURST_DATASET: Final[str] = "SLC-BURST"
ASF_PAGE_LIMIT: Final[int] = 1000


class BurstSearchClient(Protocol):
    """One call: burst granules intersecting a polygon in a time window, as GeoJSON features."""

    def search(
        self, *, wkt: str, start: datetime, end: datetime, max_results: int | None
    ) -> list[dict[str, Any]]: ...


class AsfBurstSearchClient:
    """`BurstSearchClient` over `asf_search.geo_search(dataset="SLC-BURST")`."""

    def search(
        self, *, wkt: str, start: datetime, end: datetime, max_results: int | None
    ) -> list[dict[str, Any]]:
        import asf_search

        results = asf_search.geo_search(
            intersectsWith=wkt,
            dataset=BURST_DATASET,
            start=start,
            end=end,
            **({"maxResults": max_results} if max_results is not None else {}),
        )
        features: list[dict[str, Any]] = list(results.geojson()["features"])
        return features


def bbox_wkt(bbox: tuple[float, float, float, float]) -> str:
    w, s, e, n = bbox
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def listing_path(data_dir: Path, aoi_id: str, start: datetime, end: datetime) -> Path:
    return data_dir / "interim" / "watch" / "bursts" / f"{aoi_id}_{start:%Y%m%d}_{end:%Y%m%d}.json"


def write_listing(path: Path, features: Sequence[dict[str, Any]]) -> Path:
    """Write a listing deterministically (sorted by scene name) so its sha256 is stable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(features, key=lambda f: str((f.get("properties") or {}).get("sceneName", "")))
    path.write_text(json.dumps(ordered, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_listing(path: Path) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def search_in_chunks(
    client: BurstSearchClient,
    *,
    wkt: str,
    start: datetime,
    end: datetime,
    chunk_days: int = 180,
) -> list[dict[str, Any]]:
    """Search a long window in slices, because ASF caps a single response.

    Slices are half-open on the right, so a granule on a boundary is returned exactly once;
    duplicates are removed on `sceneName` regardless, since the boundary handling is ASF's.
    """
    from datetime import timedelta

    seen: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk_days), end)
        for feature in client.search(wkt=wkt, start=cursor, end=stop, max_results=None):
            name = str((feature.get("properties") or {}).get("sceneName", ""))
            if name:
                seen[name] = feature
        cursor = stop
    return [seen[k] for k in sorted(seen)]
