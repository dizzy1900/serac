"""CDSE Sentinel-2 adapter: search on the recorded page, OAuth with a fake, windowed reads."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import rasterio
from pydantic import SecretStr

from serac.adapters.eo.cdse_sentinel2 import (
    CDSE_BAND_ASSETS,
    CDSE_TOKEN_URL,
    CdseOAuthClient,
    CdseSentinel2Adapter,
    asset_https_href,
    cdse_item_to_candidate,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.errors import CredentialsMissingError
from serac.ports.ingest import IngestRequest
from serac.settings import SeracSettings

AOI = "chamoli-rishiganga"
BBOX = (79.68, 30.33, 79.80, 30.42)
WINDOW = (376680.0, 3359180.0, 379240.0, 3361740.0)
T0 = datetime(2021, 2, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 28, 23, 59, 59, tzinfo=UTC)
EARTHSEARCH_SCENE = "S2B_44RLU_20210210_1_L2A"


def settings(**kw: Any) -> SeracSettings:
    return SeracSettings(_env_file=None, **kw)  # type: ignore[call-arg]


class FakeStac:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def search_items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        max_cloud = kwargs.get("max_cloud")
        out = [json.loads(json.dumps(i)) for i in self.items]
        if max_cloud is not None:
            out = [i for i in out if i["properties"]["eo:cloud_cover"] <= max_cloud]
        return out


class FakeTokenHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        self.posts.append((url, data))
        return {"access_token": f"tok-{len(self.posts)}", "expires_in": 600, "token_type": "Bearer"}


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        return "fake-token"


@pytest.fixture(scope="module")
def page(fixtures_dir: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (fixtures_dir / "cdse/chamoli_s2_search_2021-02.json").read_text()
    )
    return doc


def local_opener(fixtures_dir: Path, items: list[dict[str, Any]], seen: list[tuple[str, str]]):
    """Serve the committed Earth Search crops in place of the CDSE JP2 assets (a fake)."""
    band_for_href: dict[str, str] = {}
    for item in items:
        for stem, key in CDSE_BAND_ASSETS.items():
            href = asset_https_href(item, key)
            if href:
                band_for_href[href] = stem

    def _open(href: str, token: str) -> AbstractContextManager[Any]:
        seen.append((href, token))
        stem = band_for_href[href]
        return rasterio.open(fixtures_dir / "sentinel2" / AOI / EARTHSEARCH_SCENE / f"{stem}.tif")

    return _open


def test_asset_hrefs_and_candidates(page: dict[str, Any]) -> None:
    item = page["features"][0]
    href = asset_https_href(item, "SCL_20m")
    assert href and href.startswith("https://download.dataspace.copernicus.eu/odata/v1/Products(")
    assert asset_https_href(item, "nope") is None
    cand = cdse_item_to_candidate(item)
    assert cand.processing_baseline == item["properties"]["processing:version"]
    assert cand.tile_cloud_cover == item["properties"]["eo:cloud_cover"]


def test_search_and_plan_on_recorded_page(page: dict[str, Any]) -> None:
    fake = FakeStac(page["features"])
    adapter = CdseSentinel2Adapter(fake, settings=settings(), git_sha=None)
    request = IngestRequest(
        aoi_id=AOI,
        bbox_4326=BBOX,
        time_start=T0,
        time_end=T1,
        params={"max_cloud": None, "window_bounds": WINDOW},
    )
    products = adapter.search(request)
    assert len(products) == page["numberReturned"] == 5
    assert fake.calls[0]["collection"] == "sentinel-2-l2a"
    assert fake.calls[0]["datetime_range"] == "2021-02-01T00:00:00Z/2021-02-28T23:59:59Z"
    for p in products:
        assert p.source is DataSource.sentinel2_cdse and p.product_level == "L2A"
        assert set(p.assets) == {"B03", "B11", "SCL"}
        assert p.properties["proj:epsg"] == 32644
    plan = adapter.plan(request)
    # 256x256 uint16 + 128x128 uint16 + 128x128 uint8 per scene, plus the item JSON
    per_scene_pixels = 256 * 256 * 2 + 128 * 128 * 2 + 128 * 128
    assert plan.estimated_bytes is not None
    assert plan.estimated_bytes > 5 * per_scene_pixels
    assert [c.name for c in plan.requires_credentials] == ["CDSE OAuth client credentials"]
    best = adapter.search(
        request.model_copy(update={"params": {**request.params, "max_scenes": 2}})
    )
    assert len(best) == 2


def test_fetch_without_credentials_records_not_fetched(
    page: dict[str, Any], tmp_path: Path
) -> None:
    adapter = CdseSentinel2Adapter(FakeStac(page["features"]), settings=settings(), git_sha=None)
    plan = adapter.plan(
        IngestRequest(
            aoi_id=AOI, bbox_4326=BBOX, time_start=T0, time_end=T1, params={"max_cloud": None}
        )
    )
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    with pytest.raises(CredentialsMissingError, match="CDSE"):
        adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    rows = list(ledger.entries())
    assert len(rows) == 5 and {r.status for r in rows} == {ManifestStatus.not_fetched}
    assert not (tmp_path / "raw").exists()


def test_oauth_client_credentials_flow_caches_token() -> None:
    http = FakeTokenHttp()
    clock = [1000.0]
    client = CdseOAuthClient("cid", "secret", http=http, clock=lambda: clock[0])
    assert client.token() == "tok-1"
    assert client.token() == "tok-1"  # cached
    url, data = http.posts[0]
    assert url == CDSE_TOKEN_URL
    assert data == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "secret",
    }
    clock[0] += 600.0  # past expiry minus the safety margin
    assert client.token() == "tok-2" and len(http.posts) == 2


def test_oauth_rejects_bodies_without_token() -> None:
    class Bad:
        def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
            return {"error": "invalid_client"}

    with pytest.raises(RuntimeError, match="access_token"):
        CdseOAuthClient("cid", "secret", http=Bad()).token()


def test_fetch_with_credentials_reads_windows_with_bearer(
    page: dict[str, Any], fixtures_dir: Path, tmp_path: Path
) -> None:
    items = page["features"][:1]
    seen: list[tuple[str, str]] = []
    tokens = FakeTokens()
    creds = settings(cdse_client_id=SecretStr("cid"), cdse_client_secret=SecretStr("sec"))
    adapter = CdseSentinel2Adapter(
        FakeStac(items),
        token_provider=tokens,
        raster_opener=local_opener(fixtures_dir, items, seen),
        settings=creds,
        repo_root=tmp_path,
        git_sha=None,
    )
    request = IngestRequest(
        aoi_id=AOI,
        bbox_4326=BBOX,
        time_start=T0,
        time_end=T1,
        params={"max_cloud": None, "window_bounds": WINDOW},
    )
    plan = adapter.plan(request)
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    entries = adapter.fetch(plan, dest_root=tmp_path, ledger=ledger, confirm=lambda _q: True)
    kinds = [e.params["kind"] for e in entries]
    assert kinds == ["stac_item", "band", "band", "band"]
    assert all(t == "fake-token" for _h, t in seen) and tokens.calls >= 3
    scl = next(e for e in entries if e.params.get("band") == "SCL")
    assert scl.params["window_shape"] == [128, 128] and scl.params["window_bounds_epsg"] == 32644
    assert scl.path and (tmp_path / scl.path).exists()
    with rasterio.open(tmp_path / scl.path) as ds:
        assert ds.dtypes == ("uint8",) and ds.crs.to_epsg() == 32644
    b03 = next(e for e in entries if e.params.get("band") == "B03")
    assert b03.params["window_shape"] == [256, 256]
    assert b03.url and b03.url.startswith("https://download.dataspace.copernicus.eu/")
    assert all(
        e.status is ManifestStatus.fetched and e.source is DataSource.sentinel2_cdse
        for e in entries
    )
