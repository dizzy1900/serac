#!/usr/bin/env python
"""Fetch serac's credential-free EO fixtures (real bytes) and record them in the ledger.

Everything this script writes under `data/fixtures/` is real data read from a public service
at run time, hashed, and recorded in `data/manifest.jsonl` with `adapter="fixture-fetch"`.
`data/fixtures/FIXTURES.md` is regenerated from those ledger entries at the end of every run.

Idempotent: a fixture whose bytes already match its latest ledger entry is skipped; a file
that exists with different bytes (or with no ledger entry) makes the script refuse, unless
`--force` is given. Nothing here is synthetic and nothing here guesses: when a service cannot
be reached the fixture is recorded as `not_fetched` with the error in `notes`.

Usage:
    uv run python scripts/fetch_eo_fixtures.py [--only dem,s2,asf,nisar,cdse] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from serac.adapters.eo._http import make_httpx_client, sha256_and_size
from serac.adapters.eo.dem_glo30 import GLO30_LICENCE, GLO30_LICENCE_URL, Glo30DemAdapter
from serac.adapters.eo.earthsearch_sentinel2 import (
    EARTH_SEARCH_URL,
    S2_L2A_COLLECTION,
    SENTINEL_LICENCE,
    SENTINEL_LICENCE_URL,
    EarthSearchSentinel2Adapter,
    PystacSearchClient,
)
from serac.adapters.eo.s2_cloud import (
    CLOUD_ONLY_CLASSES,
    SceneCandidate,
    class_histogram,
    cloud_fraction,
    collapse_reprocessings,
    select_scenes,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.ports.ingest import Bbox4326, IngestRequest
from serac.ports.ledger import ManifestLedger

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FIXTURES_DIR = DATA_DIR / "fixtures"
LEDGER_PATH = DATA_DIR / "manifest.jsonl"
FIXTURES_MD = FIXTURES_DIR / "FIXTURES.md"
EO_SECTION_HEADING = "## EO fixtures"
ADAPTER_NAME = "fixture-fetch"
ADAPTER_VERSION = "0.1.0"

# --- design choices (not observations) -------------------------------------------------------
# Source-zone rectangles chosen for this project; the formal AOI files live in data/aoi/.
AOIS: dict[str, dict[str, Any]] = {
    "lhende-khola-trishuli": {
        "bbox": (85.51, 28.27, 85.53, 28.29),
        "epsg": 32645,
        "buffer_m": 2000.0,
    },
    "chamoli-rishiganga": {"bbox": (79.68, 30.33, 79.80, 30.42), "epsg": 32644, "buffer_m": 0.0},
    "blatten-lotschental": {"bbox": (7.78, 46.39, 7.87, 46.45), "epsg": 32632, "buffer_m": 0.0},
}
S2_AOI = "chamoli-rishiganga"
# 2 560 m square in EPSG:32644, edges on the Sentinel-2 20 m grid (tile origin 300000/3400020),
# centred near Ronti Peak (79.73 E, 30.37 N; UTM ~377963 E, 3360469 N): 256x256 px at 10 m.
S2_WINDOW_UTM: tuple[float, float, float, float] = (376680.0, 3359180.0, 379240.0, 3361740.0)
S2_SEARCH_START = datetime(2021, 1, 15, tzinfo=UTC)
S2_SEARCH_END = datetime(2021, 2, 20, 23, 59, 59, tzinfo=UTC)
S2_EVENT_DATE = datetime(2021, 2, 7, tzinfo=UTC)  # Chamoli rock-ice avalanche, 2021-02-07
S2_POST_END = datetime(2021, 2, 12, tzinfo=UTC)  # first post-event pass is 2021-02-10
S2_N_PRE = 2
S2_N_POST = 1
ASF_S1_WINDOW = ("2021-01-01T00:00:00Z", "2021-02-28T23:59:59Z")
CDSE_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
CDSE_WINDOW = ("2021-02-01T00:00:00Z", "2021-02-28T23:59:59Z")
ASF_SEARCH_URL = "https://api.daac.asf.alaska.edu/services/search/param"
NASA_DATA_POLICY_URL = (
    "https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/"
    "data-information-guidance"
)
NASA_LICENCE = "NASA Earth science data: free and open (NASA data and information policy)"
NISAR_STRIPPED_PROPERTIES = ("s3Urls", "additionalUrls", "browse", "bytes")
"""Per-file URL/size lists (~17 KB per NISAR feature); dropped to keep the fixture small."""


def log(message: str) -> None:
    sys.stdout.write(message + "\n")


def now() -> datetime:
    return datetime.now(tz=UTC)


def rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def bbox_wkt(bbox: Bbox4326) -> str:
    w, s, e, n = bbox
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def latest_fixture_entries(ledger: ManifestLedger) -> dict[str, ManifestEntry]:
    """Latest ledger entry per repo-relative path among `fixture-fetch` entries."""
    out: dict[str, ManifestEntry] = {}
    for e in ledger.entries():
        if e.adapter == ADAPTER_NAME and e.path is not None:
            prev = out.get(e.path)
            if prev is None or e.recorded_at >= prev.recorded_at:
                out[e.path] = e
    return out


class RefuseOverwriteError(RuntimeError):
    pass


def check_existing(paths: Iterable[Path], known: dict[str, ManifestEntry], force: bool) -> bool:
    """True when every path exists and matches the ledger (skip); False when none exists.

    Raises `RefuseOverwriteError` for the mixed/mismatched cases unless `force`.
    """
    paths = list(paths)
    present = [p for p in paths if p.exists()]
    if not present:
        return False
    problems: list[str] = []
    for p in paths:
        if not p.exists():
            problems.append(f"{rel(p)} missing while siblings exist")
            continue
        entry = known.get(rel(p))
        if entry is None or entry.sha256 is None:
            problems.append(f"{rel(p)} exists but has no ledger entry")
            continue
        sha, _size = sha256_and_size(p)
        if sha != entry.sha256:
            problems.append(f"{rel(p)} differs from ledger sha256 {entry.sha256[:12]}")
    if problems and not force:
        raise RefuseOverwriteError("; ".join(problems))
    if problems:
        log(f"  --force: overwriting ({'; '.join(problems)})")
        return False
    log(f"  up to date: {', '.join(rel(p) for p in paths)}")
    return True


def write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def listing_entry(
    *,
    source: DataSource,
    product_id: str,
    path: Path,
    url: str,
    params: dict[str, Any],
    licence: str,
    licence_source_url: str,
    bbox: Bbox4326,
    aoi_id: str,
    time_start: datetime | None,
    time_end: datetime | None,
    notes: str,
    status: ManifestStatus = ManifestStatus.listed,
) -> ManifestEntry:
    sha, size = sha256_and_size(path)
    return ManifestEntry(
        source=source,
        product_id=product_id,
        aoi_id=aoi_id,
        path=rel(path),
        url=url,
        params=params,
        sha256=sha,
        size_bytes=size,
        retrieved_at=now(),
        licence=licence,
        licence_source_url=licence_source_url,
        provenance=Provenance.real,
        status=status,
        time_start=time_start,
        time_end=time_end,
        bbox_4326=bbox,
        adapter=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        serac_git_sha=git_sha(),
        notes=notes,
    )


def not_fetched_entry(
    *,
    source: DataSource,
    product_id: str,
    url: str,
    licence: str,
    licence_source_url: str,
    bbox: Bbox4326,
    aoi_id: str,
    notes: str,
) -> ManifestEntry:
    return ManifestEntry(
        source=source,
        product_id=product_id,
        aoi_id=aoi_id,
        url=url,
        licence=licence,
        licence_source_url=licence_source_url,
        provenance=Provenance.real,
        status=ManifestStatus.not_fetched,
        bbox_4326=bbox,
        adapter=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        serac_git_sha=git_sha(),
        notes=notes,
    )


def git_sha() -> str | None:
    from serac.adapters.eo._base import serac_git_sha

    return serac_git_sha(str(REPO_ROOT))


# --- fixture adapters: the real adapters, writing under data/fixtures/ ------------------------


class FixtureDemAdapter(Glo30DemAdapter):
    adapter_name: ClassVar[str] = ADAPTER_NAME

    def product_dir(self, dest_root: Path, aoi_id: str, product_id: str) -> Path:
        return dest_root / "fixtures" / "dem_glo30" / aoi_id


class FixtureS2Adapter(EarthSearchSentinel2Adapter):
    adapter_name: ClassVar[str] = ADAPTER_NAME

    def product_dir(self, dest_root: Path, aoi_id: str, product_id: str) -> Path:
        return dest_root / "fixtures" / "sentinel2" / aoi_id / product_id


def confirm_small(question: str) -> bool:
    log(f"  confirmation requested: {question}")
    return False  # fixtures are small by construction; anything at the gate is a bug


# --- DEM --------------------------------------------------------------------------------------


def fetch_dem(ledger: ManifestLedger, known: dict[str, ManifestEntry], force: bool) -> None:
    adapter = FixtureDemAdapter(repo_root=REPO_ROOT)
    for aoi_id, spec in AOIS.items():
        log(f"[dem] {aoi_id}")
        request = IngestRequest(
            aoi_id=aoi_id, bbox_4326=spec["bbox"], params={"buffer_m": spec["buffer_m"]}
        )
        target = adapter.product_dir(DATA_DIR, aoi_id, "") / "glo30_crop.tif"
        if check_existing([target], known, force):
            continue
        plan = adapter.plan(request)
        log(f"  plan: {plan.estimate_basis} -> {plan.estimated_bytes:,} B uncompressed")
        entries = adapter.fetch(plan, dest_root=DATA_DIR, ledger=ledger, confirm=confirm_small)
        for e in entries:
            log(
                f"  fetched {e.path} {e.size_bytes:,} B sha256 {e.sha256[:12] if e.sha256 else '-'}"
            )


# --- Sentinel-2 via Earth Search ----------------------------------------------------------------


def fetch_s2(ledger: ManifestLedger, known: dict[str, ManifestEntry], force: bool) -> None:
    spec = AOIS[S2_AOI]
    bbox: Bbox4326 = spec["bbox"]
    adapter = FixtureS2Adapter(PystacSearchClient(EARTH_SEARCH_URL), repo_root=REPO_ROOT)
    base_params = {"window_bounds": list(S2_WINDOW_UTM), "max_cloud": None}
    search_request = IngestRequest(
        aoi_id=S2_AOI,
        bbox_4326=bbox,
        time_start=S2_SEARCH_START,
        time_end=S2_SEARCH_END,
        params={**base_params, "keep_reprocessings": True},
    )
    log(f"[s2] search {S2_SEARCH_START.date()}..{S2_SEARCH_END.date()} over {S2_AOI}")
    products = adapter.search(search_request)
    log(f"  {len(products)} items (reprocessings kept for the candidate table)")

    # Rank every candidate by the AOI-window cloud fraction from its SCL (real reads).
    rows: list[dict[str, Any]] = []
    candidates: list[SceneCandidate] = []
    for p in products:
        scl = adapter.read_scl_window(p, bbox, window_bounds=S2_WINDOW_UTM)
        fraction = cloud_fraction(scl)
        cloud_only = cloud_fraction(scl, classes=CLOUD_ONLY_CLASSES)
        assert p.time_start is not None
        cand = SceneCandidate(
            product_id=p.product_id,
            acquired=p.time_start,
            tile_cloud_cover=p.properties.get("eo:cloud_cover"),
            aoi_cloud_fraction=fraction,
            processing_baseline=p.properties.get("s2:processing_baseline"),
        )
        candidates.append(cand)
        rows.append(
            {
                "product_id": p.product_id,
                "datetime": p.time_start.isoformat(),
                "eo:cloud_cover": p.properties.get("eo:cloud_cover"),
                "s2:processing_baseline": p.properties.get("s2:processing_baseline"),
                "aoi_cloud_shadow_snow_fraction": fraction,
                "aoi_cloud_only_fraction": cloud_only,
                "scl_histogram": class_histogram(scl),
                "selected_role": None,
            }
        )
        log(f"  {p.product_id} tile {cand.tile_cloud_cover:6.2f}% aoi {fraction!r}")
    collapsed = collapse_reprocessings(candidates)
    pre = select_scenes([c for c in collapsed if c.acquired < S2_EVENT_DATE], n=S2_N_PRE)
    post = select_scenes(
        [c for c in collapsed if S2_EVENT_DATE <= c.acquired <= S2_POST_END], n=S2_N_POST
    )
    roles = {c.product_id: "pre" for c in pre} | {c.product_id: "post" for c in post}
    for row in rows:
        row["selected_role"] = roles.get(row["product_id"])
    log(f"  selected pre: {[c.product_id for c in pre]} post: {[c.product_id for c in post]}")

    cand_path = FIXTURES_DIR / "sentinel2" / S2_AOI / "candidates.json"
    doc = {
        "aoi_id": S2_AOI,
        "stac_url": EARTH_SEARCH_URL,
        "collection": S2_L2A_COLLECTION,
        "search_bbox_4326": list(bbox),
        "search_window": [S2_SEARCH_START.isoformat(), S2_SEARCH_END.isoformat()],
        "aoi_window_epsg": spec["epsg"],
        "aoi_window_bounds": list(S2_WINDOW_UTM),
        "cloud_shadow_snow_classes": [3, 8, 9, 10, 11],
        "cloud_only_classes": sorted(CLOUD_ONLY_CLASSES),
        "selection": {
            "split_date": S2_EVENT_DATE.isoformat(),
            "post_window_end": S2_POST_END.isoformat(),
            "n_pre": S2_N_PRE,
            "n_post": S2_N_POST,
            "rule": (
                "collapse reprocessings (newest baseline), rank by AOI cloud/shadow/snow fraction"
            ),
        },
        "candidates": rows,
    }
    if not check_existing([cand_path], known, force):
        write_json(cand_path, doc)
        ledger.append(
            listing_entry(
                source=DataSource.sentinel2_earthsearch,
                product_id=f"earthsearch-candidates-{S2_SEARCH_START.date()}_{S2_SEARCH_END.date()}",
                path=cand_path,
                url=f"{EARTH_SEARCH_URL}/search",
                params={
                    "collection": S2_L2A_COLLECTION,
                    "bbox": list(bbox),
                    "window_bounds": list(S2_WINDOW_UTM),
                },
                licence=SENTINEL_LICENCE,
                licence_source_url=SENTINEL_LICENCE_URL,
                bbox=bbox,
                aoi_id=S2_AOI,
                time_start=S2_SEARCH_START,
                time_end=S2_SEARCH_END,
                notes=(
                    "STAC search results ranked by the AOI-window SCL cloud fraction (real "
                    "windowed reads); the selection table for the committed scene fixtures"
                ),
            )
        )
    else:
        write_json(cand_path, doc) if force else None

    by_id = {p.product_id: p for p in products}
    for cand in [*pre, *post]:
        product = by_id[cand.product_id]
        log(f"[s2] {product.product_id} ({roles[product.product_id]})")
        dest = adapter.product_dir(DATA_DIR, S2_AOI, product.product_id)
        targets = [dest / "item.json", dest / "SCL.tif", dest / "B03.tif", dest / "B11.tif"]
        if check_existing(targets, known, force):
            continue
        request = IngestRequest(
            aoi_id=S2_AOI,
            bbox_4326=bbox,
            time_start=cand.acquired,
            time_end=cand.acquired,
            event_id="chamoli-2021",
            params={**base_params, "selected_role": roles[product.product_id]},
        )
        est = adapter.estimate_product_bytes(product, bbox, S2_WINDOW_UTM)
        plan = adapter.build_plan(
            request,
            [product.model_copy(update={"estimated_bytes": est})],
            estimated_bytes=est,
            estimate_basis="fixture window pixels x bytes per sample, uncompressed, plus item JSON",
        )
        entries = adapter.fetch(plan, dest_root=DATA_DIR, ledger=ledger, confirm=confirm_small)
        for e in entries:
            log(
                f"  fetched {e.path} {e.size_bytes:,} B sha256 {e.sha256[:12] if e.sha256 else '-'}"
            )


# --- ASF listings (Sentinel-1, NISAR) -----------------------------------------------------------


def fetch_asf_s1(ledger: ManifestLedger, known: dict[str, ManifestEntry], force: bool) -> None:
    import asf_search as asf

    aoi_id = "chamoli-rishiganga"
    bbox: Bbox4326 = AOIS[aoi_id]["bbox"]
    path = FIXTURES_DIR / "asf" / "chamoli_s1_2021-01-01_2021-02-28.geojson"
    log(f"[asf] Sentinel-1 IW SLC+GRD_HD listing over {aoi_id}")
    if check_existing([path], known, force):
        return
    params: dict[str, Any] = {
        "intersectsWith": bbox_wkt(bbox),
        "platform": ["Sentinel-1"],
        "processingLevel": ["SLC", "GRD_HD"],
        "beamMode": ["IW"],
        "start": ASF_S1_WINDOW[0],
        "end": ASF_S1_WINDOW[1],
    }
    try:
        results = asf.geo_search(
            intersectsWith=params["intersectsWith"],
            platform=[asf.PLATFORM.SENTINEL1],
            processingLevel=[asf.PRODUCT_TYPE.SLC, asf.PRODUCT_TYPE.GRD_HD],
            beamMode=[asf.BEAMMODE.IW],
            start=params["start"],
            end=params["end"],
        )
        doc = results.geojson()
    except Exception as exc:
        ledger.append(
            not_fetched_entry(
                source=DataSource.sentinel1_asf,
                product_id="asf-s1-listing-chamoli-2021-01_2021-02",
                url=ASF_SEARCH_URL,
                licence=SENTINEL_LICENCE,
                licence_source_url=SENTINEL_LICENCE_URL,
                bbox=bbox,
                aoi_id=aoi_id,
                notes=f"asf_search geo_search failed: {type(exc).__name__}: {exc}"[:500],
            )
        )
        log(f"  NOT FETCHED: {exc}")
        return
    write_json(path, doc)
    ledger.append(
        listing_entry(
            source=DataSource.sentinel1_asf,
            product_id="asf-s1-listing-chamoli-2021-01_2021-02",
            path=path,
            url=ASF_SEARCH_URL,
            params={**params, "asf_search_version": asf.__version__, "count": len(doc["features"])},
            licence=SENTINEL_LICENCE,
            licence_source_url=SENTINEL_LICENCE_URL,
            bbox=bbox,
            aoi_id=aoi_id,
            time_start=datetime.fromisoformat(ASF_S1_WINDOW[0].replace("Z", "+00:00")),
            time_end=datetime.fromisoformat(ASF_S1_WINDOW[1].replace("Z", "+00:00")),
            notes=(
                "asf_search ASFSearchResults.geojson() verbatim (keys sorted); listing metadata "
                "served by ASF DAAC / NASA CMR; no product bytes downloaded (Earthdata Login "
                "not available)"
            ),
        )
    )
    log(f"  {len(doc['features'])} granules -> {rel(path)}")


def fetch_asf_nisar(ledger: ManifestLedger, known: dict[str, ManifestEntry], force: bool) -> None:
    import asf_search as asf

    aoi_id = "lhende-khola-trishuli"
    bbox: Bbox4326 = AOIS[aoi_id]["bbox"]
    today = now().date().isoformat()
    path = FIXTURES_DIR / "asf" / f"nisar_probe_{today}.json"
    log(f"[asf] NISAR probe over {aoi_id}")
    if check_existing([path], known, force):
        return
    params: dict[str, Any] = {"intersectsWith": bbox_wkt(bbox), "platform": ["NISAR"]}
    try:
        results = asf.geo_search(intersectsWith=params["intersectsWith"], platform=["NISAR"])
        doc = results.geojson()
    except Exception as exc:
        ledger.append(
            not_fetched_entry(
                source=DataSource.nisar_asf,
                product_id=f"asf-nisar-probe-lhende-{today}",
                url=ASF_SEARCH_URL,
                licence=NASA_LICENCE,
                licence_source_url=NASA_DATA_POLICY_URL,
                bbox=bbox,
                aoi_id=aoi_id,
                notes=f"asf_search geo_search failed: {type(exc).__name__}: {exc}"[:500],
            )
        )
        log(f"  NOT FETCHED: {exc}")
        return
    summary: dict[str, int] = {}
    for f in doc["features"]:
        props = f["properties"]
        for key in NISAR_STRIPPED_PROPERTIES:
            props.pop(key, None)
        k = f"{props.get('processingLevel')}/{props.get('productionConfiguration')}"
        summary[k] = summary.get(k, 0) + 1
    times = sorted(str(f["properties"].get("startTime")) for f in doc["features"])
    doc["serac_probe"] = {
        "retrieved_at": now().isoformat(),
        "query": params,
        "asf_search_version": asf.__version__,
        "total_features": len(doc["features"]),
        "stripped_properties": list(NISAR_STRIPPED_PROPERTIES),
        "by_level_and_production_configuration": dict(sorted(summary.items())),
        "earliest_start": times[0] if times else None,
        "latest_start": times[-1] if times else None,
    }
    write_json(path, doc)
    ledger.append(
        listing_entry(
            source=DataSource.nisar_asf,
            product_id=f"asf-nisar-probe-lhende-{today}",
            path=path,
            url=ASF_SEARCH_URL,
            params={
                **params,
                "asf_search_version": asf.__version__,
                "count": len(doc["features"]),
                "stripped_properties": list(NISAR_STRIPPED_PROPERTIES),
                "by_level": summary,
            },
            licence=NASA_LICENCE + "; listing metadata via ASF DAAC / NASA CMR",
            licence_source_url=NASA_DATA_POLICY_URL,
            bbox=bbox,
            aoi_id=aoi_id,
            time_start=datetime.fromisoformat(times[0].replace("Z", "+00:00")) if times else None,
            time_end=datetime.fromisoformat(times[-1].replace("Z", "+00:00")) if times else None,
            notes=(
                f"asf_search geo_search(platform=NISAR) as of {today}; per-file URL lists "
                f"({', '.join(NISAR_STRIPPED_PROPERTIES)}) stripped for size; no product bytes "
                "downloaded (Earthdata Login not available)"
            ),
        )
    )
    log(f"  {len(doc['features'])} granules {summary} -> {rel(path)}")


# --- CDSE STAC search --------------------------------------------------------------------------


def fetch_cdse(ledger: ManifestLedger, known: dict[str, ManifestEntry], force: bool) -> None:
    aoi_id = "chamoli-rishiganga"
    bbox: Bbox4326 = AOIS[aoi_id]["bbox"]
    path = FIXTURES_DIR / "cdse" / "chamoli_s2_search_2021-02.json"
    log(f"[cdse] STAC search over {aoi_id}")
    if check_existing([path], known, force):
        return
    body = {
        "collections": ["sentinel-2-l2a"],
        "bbox": list(bbox),
        "datetime": f"{CDSE_WINDOW[0]}/{CDSE_WINDOW[1]}",
        "limit": 50,
    }
    url = f"{CDSE_STAC_URL}/search"
    try:
        with make_httpx_client() as client:
            response = client.post(url, json=body)
            response.raise_for_status()
            doc = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        ledger.append(
            not_fetched_entry(
                source=DataSource.sentinel2_cdse,
                product_id="cdse-stac-search-chamoli-2021-02",
                url=url,
                licence=SENTINEL_LICENCE,
                licence_source_url=SENTINEL_LICENCE_URL,
                bbox=bbox,
                aoi_id=aoi_id,
                notes=f"CDSE STAC search failed: {type(exc).__name__}: {exc}"[:500],
            )
        )
        log(f"  NOT FETCHED: {exc}")
        return
    write_json(path, doc)
    ledger.append(
        listing_entry(
            source=DataSource.sentinel2_cdse,
            product_id="cdse-stac-search-chamoli-2021-02",
            path=path,
            url=url,
            params={"request_body": body, "count": len(doc.get("features", []))},
            licence=SENTINEL_LICENCE,
            licence_source_url=SENTINEL_LICENCE_URL,
            bbox=bbox,
            aoi_id=aoi_id,
            time_start=datetime.fromisoformat(CDSE_WINDOW[0].replace("Z", "+00:00")),
            time_end=datetime.fromisoformat(CDSE_WINDOW[1].replace("Z", "+00:00")),
            notes=(
                "CDSE STAC POST /search response verbatim (keys sorted); public search, "
                "product downloads need CDSE OAuth (not available)"
            ),
        )
    )
    log(f"  {len(doc.get('features', []))} items -> {rel(path)}")


# --- FIXTURES.md --------------------------------------------------------------------------------


def write_fixtures_md(ledger: ManifestLedger) -> None:
    latest = latest_fixture_entries(ledger)
    lines = [
        EO_SECTION_HEADING,
        "",
        "Real bytes read from public services by `scripts/fetch_eo_fixtures.py`; every row has a",
        "matching `data/manifest.jsonl` entry (`adapter: fixture-fetch`) whose sha256 is verified",
        "offline by `tests/unit/test_eo_fixture_integrity.py`. Nothing here is synthetic.",
        "",
        "### Design choices (not observations)",
        "",
        "Source-zone bounding boxes used for the crops, chosen for this project (W, S, E, N):",
        "",
    ]
    for aoi_id, spec in AOIS.items():
        w, s, e, n = spec["bbox"]
        lines.append(
            f"- `{aoi_id}`: {w}, {s}, {e}, {n} (cube EPSG:{spec['epsg']}); DEM crop buffer "
            f"{spec['buffer_m']:g} m"
        )
    lines += [
        "",
        f"Sentinel-2 fixture window: {S2_WINDOW_UTM[0]:.0f}..{S2_WINDOW_UTM[2]:.0f} E, "
        f"{S2_WINDOW_UTM[1]:.0f}..{S2_WINDOW_UTM[3]:.0f} N in EPSG:{AOIS[S2_AOI]['epsg']} "
        "(2 560 m square on the 20 m grid, 256x256 px at 10 m / 128x128 px at 20 m), centred near "
        "Ronti Peak; smaller than the AOI bbox so each band stays under ~150 KB. DEM crops are",
        "the AOI bbox (plus buffer) snapped outward to the GLO-30 1 arc-second grid, EPSG:4326",
        "float32 as delivered. S2 scene selection: reprocessings collapsed to the newest",
        "baseline, ranked by the fraction of SCL classes {3, 8, 9, 10, 11} over the fixture",
        f"window; two pre-event and one post-event scene around {S2_EVENT_DATE.date()}",
        f"(see `sentinel2/{S2_AOI}/candidates.json`).",
        "",
        "### Files",
        "",
        "| path | source URL | retrieved_at | sha256 | size (B) | licence |",
        "|---|---|---|---|---|---|",
    ]
    for path in sorted(latest):
        e = latest[path]
        if e.status not in (ManifestStatus.fetched, ManifestStatus.listed):
            continue
        url = e.url or "-"
        when = e.retrieved_at.isoformat() if e.retrieved_at else "-"
        lic = (
            f"[{e.licence.split(':')[0].split('(')[0].strip()}]({e.licence_source_url})"
            if e.licence_source_url
            else e.licence
        )
        lines.append(f"| `{path}` | {url} | {when} | `{e.sha256}` | {e.size_bytes} | {lic} |")
    missing = [
        e
        for e in ledger.entries()
        if e.adapter == ADAPTER_NAME and e.status == ManifestStatus.not_fetched
    ]
    if missing:
        lines += ["", "### Not fetched", ""]
        for e in missing:
            lines.append(f"- `{e.source.value}` `{e.product_id}`: {e.notes}")
    lines += [
        "",
        "### Licences",
        "",
        f"- Copernicus DEM GLO-30: {GLO30_LICENCE}. Terms: {GLO30_LICENCE_URL}",
        f"- Copernicus Sentinel data: {SENTINEL_LICENCE}. Legal notice: {SENTINEL_LICENCE_URL}",
        f"- NASA NISAR listing metadata: {NASA_LICENCE}. Policy: {NASA_DATA_POLICY_URL}",
        "",
    ]
    replace_eo_section(FIXTURES_MD, "\n".join(lines))
    log(f"[md] rewrote the EO section of {rel(FIXTURES_MD)} ({sum(1 for _ in latest)} files)")


def replace_eo_section(path: Path, section: str) -> None:
    """Rewrite only the `## EO fixtures` section; other lanes own the rest of FIXTURES.md."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    start = existing.find(EO_SECTION_HEADING)
    if start < 0:
        merged = existing.rstrip("\n") + ("\n\n" if existing.strip() else "") + section
    else:
        after = existing.find("\n## ", start + len(EO_SECTION_HEADING))
        tail = existing[after + 1 :] if after >= 0 else ""
        merged = existing[:start] + section.rstrip("\n") + "\n" + ("\n" + tail if tail else "")
    path.write_text(merged.rstrip("\n") + "\n", encoding="utf-8")


# --- main --------------------------------------------------------------------------------------

STEPS = {
    "dem": fetch_dem,
    "s2": fetch_s2,
    "asf": fetch_asf_s1,
    "nisar": fetch_asf_nisar,
    "cdse": fetch_cdse,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only", default=",".join(STEPS), help="comma-separated subset of: " + ", ".join(STEPS)
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite fixtures whose bytes differ"
    )
    args = parser.parse_args(argv)
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        parser.error(f"unknown step(s): {unknown}")
    ledger = JsonlManifestLedger(LEDGER_PATH)
    failures = 0
    for step in steps:
        known = latest_fixture_entries(ledger)
        try:
            STEPS[step](ledger, known, args.force)
        except RefuseOverwriteError as exc:
            log(f"  REFUSED (use --force to overwrite): {exc}")
            failures += 1
    write_fixtures_md(ledger)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
