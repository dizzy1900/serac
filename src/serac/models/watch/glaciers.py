"""RGI 7.0 glacier outlines, needed because `SlopeUnit.glacier_cover` is a non-nullable bool.

Provenance note, because it matters here. The authoritative RGI 7.0 distribution is NSIDC
dataset `nsidc0770_rgi_v7`, whose HTTPS endpoint requires an interactive Earthdata OAuth
redirect that a bearer token does not satisfy (it answers 401 to `Authorization: Bearer`).
The files fetched here are the **official RGI 7.0 regional archives** re-hosted by the
University of Bremen climate group, who produced RGI 7.0; the path is
`rgi7_data/rgi70_official/`, i.e. the released version rather than one of the beta levels also
present on that server. Every byte is hashed and ledgered with that URL, and the model card
says the copy is a mirror rather than the NSIDC original.

RGI is CC BY 4.0.

If the outlines cannot be fetched, `GlacierOutlines.available` is False, the slope-unit
parquet is still written with `glacier_cover = null`, and **no `SlopeUnit` records are
emitted**. The contract is not relaxed.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance

RGI7_BASE: Final[str] = (
    "https://cluster.klima.uni-bremen.de/~fmaussion/misc/rgi7_data/rgi70_official"
)
RGI7_GLOBAL_G: Final[str] = f"{RGI7_BASE}/RGI2000-v7.0-G-global"
RGI7_LICENCE: Final[str] = "CC-BY-4.0 (Randolph Glacier Inventory 7.0, RGI Consortium)"
RGI7_LICENCE_URL: Final[str] = "https://www.glims.org/rgi_user_guide/welcome.html"
RGI7_VERSION: Final[str] = "RGI2000-v7.0-G"

RGI7_REGIONS: Final[dict[str, tuple[float, float, float, float]]] = {
    "14_south_asia_west": (65.0, 26.0, 82.0, 39.0),
    "15_south_asia_east": (72.0, 26.0, 100.0, 32.0),
    "13_central_asia": (67.0, 27.0, 105.0, 55.0),
    "11_central_europe": (-2.0, 41.0, 20.0, 49.0),
}
"""Bounding boxes of the RGI first-order regions this repository's AOIs can fall in.

Deliberately a short, explicit table rather than a spatial join against the region polygons:
the AOIs are three fixed places, the boxes overlap so a lookup returns every candidate region,
and each candidate is then fetched and clipped, so an AOI on a region boundary is handled by
taking the union rather than by a nearest-region guess.
"""


@dataclass
class GlacierOutlines:
    """Glacier polygons over one AOI, or an honest record of why there are none."""

    available: bool
    geometries: Any = None
    regions: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    n_glaciers: int = 0
    reason: str | None = None
    parquet_path: str | None = None

    def to_crs(self, epsg: int) -> GlacierOutlines:
        """Reproject the outlines onto a projected CRS before any area comparison.

        RGI ships in EPSG:4326 and slope units live on the AOI's UTM grid. Intersecting the
        two without this step silently returns zero overlap everywhere, which would report a
        heavily glaciated AOI as having no glacier cover at all.
        """
        if not self.available or self.geometries is None:
            return self
        return GlacierOutlines(
            available=True,
            geometries=self.geometries.to_crs(f"EPSG:{epsg}"),
            regions=self.regions,
            source_urls=self.source_urls,
            source_refs=self.source_refs,
            n_glaciers=self.n_glaciers,
            parquet_path=self.parquet_path,
        )

    def cover_fraction(self, geometry: Any) -> float:
        """Fraction of a slope unit's area covered by glacier polygons; 0.0 when unavailable.

        The caller must have projected the outlines onto the slope units' CRS with `to_crs`.
        """
        if not self.available or self.geometries is None or geometry.area <= 0:
            return 0.0
        hits = self.geometries.sindex.query(geometry, predicate="intersects")
        if len(hits) == 0:
            return 0.0
        from shapely.ops import unary_union

        overlap = unary_union(list(self.geometries.geometry.iloc[hits])).intersection(geometry)
        return float(min(overlap.area / geometry.area, 1.0))

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "version": RGI7_VERSION if self.available else None,
            "regions": self.regions,
            "source_urls": self.source_urls,
            "n_glaciers_in_aoi": self.n_glaciers,
            "licence": RGI7_LICENCE if self.available else None,
            "parquet_path": self.parquet_path,
            "reason": self.reason,
        }


def regions_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Every RGI first-order region in the table whose box intersects `bbox`."""
    w, s, e, n = bbox
    out = [
        name
        for name, (rw, rs, re_, rn) in sorted(RGI7_REGIONS.items())
        if not (e < rw or w > re_ or n < rs or s > rn)
    ]
    return out


def _download(url: str, dest: Path, timeout_s: float = 900.0) -> tuple[str, int]:
    import hashlib

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(60.0, read=timeout_s), follow_redirects=True) as c,
            c.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with part.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 20):
                    digest.update(chunk)
                    size += len(chunk)
                    fh.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size


def fetch_rgi7(
    *, data_dir: Path, aoi_dir: Path, aoi_id: str, online: bool = False
) -> GlacierOutlines:
    """Glacier outlines clipped to the AOI, fetching the RGI regional archives if allowed."""
    import geopandas as gpd

    clipped_path = data_dir / "interim" / "watch" / f"glaciers_rgi7_{aoi_id}.parquet"
    if clipped_path.exists():
        frame = gpd.read_parquet(clipped_path)
        meta = json.loads((clipped_path.with_suffix(".json")).read_text(encoding="utf-8"))
        return GlacierOutlines(
            available=True,
            geometries=frame,
            regions=meta["regions"],
            source_urls=meta["source_urls"],
            source_refs=meta["source_refs"],
            n_glaciers=len(frame),
            parquet_path=clipped_path.as_posix(),
        )
    if not online:
        return GlacierOutlines(
            available=False,
            reason=(
                f"no cached RGI 7.0 clip at {clipped_path} and --online was not passed; "
                "SlopeUnit records need glacier_cover and none were written"
            ),
        )

    bbox = tuple(
        float(v)
        for v in json.loads((aoi_dir / "aoi.json").read_text(encoding="utf-8"))[
            "cube_extent_bbox_4326"
        ]
    )
    regions = regions_for_bbox(bbox)  # type: ignore[arg-type]
    if not regions:
        return GlacierOutlines(
            available=False,
            reason=f"AOI bbox {bbox} matches no RGI first-order region in the lookup table",
        )

    ledger = JsonlManifestLedger(data_dir / "manifest.jsonl")
    raw_root = data_dir / "raw" / "rgi_glaciers"
    frames = []
    urls: list[str] = []
    for region in regions:
        name = f"{RGI7_VERSION}-{region}"
        url = f"{RGI7_GLOBAL_G}/{name}.zip"
        dest = raw_root / f"{name}.zip"
        try:
            if dest.exists():
                sha, size = sha256_of_file(dest), dest.stat().st_size
            else:
                sha, size = _download(url, dest)
        except (httpx.HTTPError, OSError) as exc:
            ledger.append(
                _entry(
                    product_id=name,
                    url=url,
                    aoi_id=aoi_id,
                    status=ManifestStatus.failed,
                    notes=f"{type(exc).__name__}: {exc}"[:400],
                )
            )
            continue
        ledger.append(
            _entry(
                product_id=name,
                url=url,
                aoi_id=aoi_id,
                status=ManifestStatus.fetched,
                path=dest.relative_to(data_dir.parent).as_posix()
                if data_dir.parent in dest.parents
                else dest.as_posix(),
                sha256=sha,
                size_bytes=size,
                notes=(
                    "RGI 7.0 official regional glacier outlines, mirrored by the University of "
                    "Bremen climate group; NSIDC's own endpoint needs an interactive Earthdata "
                    "OAuth redirect that a bearer token cannot satisfy"
                ),
            )
        )
        urls.append(url)
        frames.append(_read_region(dest, bbox))  # type: ignore[arg-type]

    frames = [f for f in frames if f is not None and len(f) > 0]
    if not frames:
        return GlacierOutlines(
            available=False,
            reason=(
                f"RGI 7.0 regions {regions} were tried but yielded no glacier polygon inside "
                f"the AOI bbox; see data/manifest.jsonl for the fetch outcome"
            ),
        )
    import pandas as pd

    merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    clipped_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(clipped_path, index=False)
    source_refs = [f"rgi-7-0-{r.replace('_', '-')}" for r in regions]
    clipped_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "regions": regions,
                "source_urls": urls,
                "source_refs": source_refs,
                "version": RGI7_VERSION,
                "licence": RGI7_LICENCE,
                "n_glaciers": len(merged),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    ledger.append(
        _entry(
            product_id=f"rgi7_clip_{aoi_id}",
            url=RGI7_GLOBAL_G,
            aoi_id=aoi_id,
            status=ManifestStatus.fetched,
            path=clipped_path.as_posix(),
            sha256=sha256_of_file(clipped_path),
            size_bytes=clipped_path.stat().st_size,
            provenance=Provenance.derived,
            notes=f"RGI 7.0 outlines clipped to the {aoi_id} bbox ({len(merged)} glaciers)",
        )
    )
    return GlacierOutlines(
        available=True,
        geometries=merged,
        regions=regions,
        source_urls=urls,
        source_refs=source_refs,
        n_glaciers=len(merged),
        parquet_path=clipped_path.as_posix(),
    )


def _read_region(zip_path: Path, bbox: tuple[float, float, float, float]) -> Any:
    """Read the shapefile inside an RGI regional zip and clip it to `bbox`."""
    import geopandas as gpd

    with zipfile.ZipFile(zip_path) as zf:
        shp = next((n for n in zf.namelist() if n.endswith(".shp")), None)
    if shp is None:
        return None
    frame = gpd.read_file(f"zip://{zip_path}!{shp}", bbox=bbox)
    if frame.crs is None:
        return None
    return frame.to_crs("EPSG:4326")


def _entry(
    *,
    product_id: str,
    url: str,
    aoi_id: str,
    status: ManifestStatus,
    path: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    provenance: Provenance = Provenance.real,
    notes: str | None = None,
) -> ManifestEntry:
    now = datetime.now(tz=UTC)
    return ManifestEntry(
        source=DataSource.rgi_glaciers,
        product_id=product_id,
        product_level=RGI7_VERSION,
        aoi_id=aoi_id,
        path=path,
        url=url,
        sha256=sha256,
        size_bytes=size_bytes,
        retrieved_at=now if status == ManifestStatus.fetched else None,
        licence=RGI7_LICENCE,
        licence_source_url=RGI7_LICENCE_URL,
        provenance=provenance,
        status=status,
        adapter="rgi7_glaciers",
        adapter_version="0.1.0",
        notes=notes,
    )
