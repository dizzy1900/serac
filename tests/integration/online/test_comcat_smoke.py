"""Online: ComCat still serves the landslide query through the adapter (allowed to skip)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import require_network

from serac.adapters.seismic.usgs_comcat import ComCatCatalog
from serac.ports.seismic import CatalogQuery

pytestmark = pytest.mark.online


def test_comcat_adapter_returns_the_langtang_landslide_event() -> None:
    require_network("earthquake.usgs.gov")
    catalog = ComCatCatalog(page_size=100)
    query = CatalogQuery(
        start_utc=datetime(2026, 8, 25, tzinfo=UTC),
        end_utc=datetime(2026, 8, 27, tzinfo=UTC),
        event_type="landslide",
    )
    events = catalog.query(query)
    ids = {e.event_id for e in events}
    assert "us7000tbwb" in ids
    assert all(e.event_type == "landslide" for e in events)
