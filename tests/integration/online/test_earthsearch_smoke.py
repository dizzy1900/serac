"""Online smoke: Earth Search still lists the Chamoli scenes the fixtures were cut from."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import require_network

from serac.adapters.eo.earthsearch_sentinel2 import (
    EARTH_SEARCH_URL,
    EarthSearchSentinel2Adapter,
    PystacSearchClient,
)
from serac.ports.ingest import IngestRequest

pytestmark = pytest.mark.online

HOST = EARTH_SEARCH_URL.removeprefix("https://").split("/")[0]
CHAMOLI = (79.68, 30.33, 79.80, 30.42)


def test_search_lists_post_event_scene() -> None:
    require_network(HOST)
    adapter = EarthSearchSentinel2Adapter(PystacSearchClient(EARTH_SEARCH_URL))
    request = IngestRequest(
        aoi_id="chamoli-rishiganga",
        bbox_4326=CHAMOLI,
        time_start=datetime(2021, 2, 8, tzinfo=UTC),
        time_end=datetime(2021, 2, 12, tzinfo=UTC),
        params={"max_cloud": 40.0},
    )
    products = adapter.search(request)
    ids = [p.product_id for p in products]
    assert "S2B_44RLU_20210210_1_L2A" in ids, ids
    assert all("SCL" in p.assets for p in products)


def test_plan_is_offline_after_search() -> None:
    require_network(HOST)
    adapter = EarthSearchSentinel2Adapter(PystacSearchClient(EARTH_SEARCH_URL))
    request = IngestRequest(
        aoi_id="chamoli-rishiganga",
        bbox_4326=CHAMOLI,
        time_start=datetime(2021, 2, 8, tzinfo=UTC),
        time_end=datetime(2021, 2, 12, tzinfo=UTC),
    )
    plan = adapter.plan(request)
    assert plan.estimated_bytes is not None and plan.estimated_bytes > 0
    assert plan.refusals == []
