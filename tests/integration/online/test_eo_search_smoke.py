"""Online smoke: the public search endpoints still answer the way the fixtures were recorded.

Nothing here downloads a product or needs a credential; each test skips when the host is
unreachable. Run with `make smoke-online` (`SERAC_ONLINE=1`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import require_network

from serac.adapters.eo._asf import AsfSearchLibClient
from serac.adapters.eo.asf_sentinel1 import Sentinel1AsfAdapter, group_by_relative_orbit
from serac.adapters.eo.cdse_sentinel2 import CDSE_STAC_URL, CdseSentinel2Adapter
from serac.adapters.eo.earthsearch_sentinel2 import PystacSearchClient
from serac.adapters.eo.nisar import NisarAdapter, level_counts
from serac.adapters.eo.nisar_constraints import SCIENCE_LEVELS
from serac.ports.ingest import IngestRequest

pytestmark = pytest.mark.online

ASF_HOST = "api.daac.asf.alaska.edu"
CDSE_HOST = "stac.dataspace.copernicus.eu"
CHAMOLI = (79.68, 30.33, 79.80, 30.42)
LHENDE = (85.51, 28.27, 85.53, 28.29)


def test_asf_sentinel1_listing_matches_fixture_paths() -> None:
    require_network(ASF_HOST)
    adapter = Sentinel1AsfAdapter(AsfSearchLibClient())
    products = adapter.search(
        IngestRequest(
            aoi_id="chamoli-rishiganga",
            bbox_4326=CHAMOLI,
            time_start=datetime(2021, 1, 1, tzinfo=UTC),
            time_end=datetime(2021, 2, 28, 23, 59, 59, tzinfo=UTC),
        )
    )
    assert products, "no IW SLC granules listed"
    assert {56, 63, 129, 165} <= set(group_by_relative_orbit(products))
    assert all(p.estimated_bytes for p in products)


def test_nisar_probe_still_splits_by_collection_name() -> None:
    require_network(ASF_HOST)
    adapter = NisarAdapter(AsfSearchLibClient())
    found = adapter.search(
        IngestRequest(
            aoi_id="lhende-khola-trishuli",
            bbox_4326=LHENDE,
            params={"processing_level": sorted(SCIENCE_LEVELS)},
        )
    )
    counts = level_counts(found)
    assert counts.get("unknown", 0) == 0, counts
    assert counts.get("beta", 0) >= 72 and counts.get("provisional", 0) >= 87, counts


def test_cdse_stac_search_lists_chamoli_scenes() -> None:
    require_network(CDSE_HOST)
    adapter = CdseSentinel2Adapter(PystacSearchClient(CDSE_STAC_URL))
    products = adapter.search(
        IngestRequest(
            aoi_id="chamoli-rishiganga",
            bbox_4326=CHAMOLI,
            time_start=datetime(2021, 2, 1, tzinfo=UTC),
            time_end=datetime(2021, 2, 28, 23, 59, 59, tzinfo=UTC),
            params={"max_cloud": None},
        )
    )
    assert products
    assert all({"B03", "B11", "SCL"} <= set(p.assets) for p in products)
    assert all(p.properties["proj:epsg"] == 32644 for p in products)
