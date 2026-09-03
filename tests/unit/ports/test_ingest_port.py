from __future__ import annotations

from datetime import UTC, datetime

import pytest

from serac.domain.manifest import DataSource
from serac.ports.ingest import (
    CredentialSpec,
    DryRunPlan,
    IngestAdapter,
    IngestRequest,
    ProductRecord,
)

BBOX = (79.68, 30.33, 79.80, 30.42)
T0 = datetime(2021, 2, 1, tzinfo=UTC)
T1 = datetime(2021, 2, 10, tzinfo=UTC)


def _product(**kw: object) -> ProductRecord:
    base: dict[str, object] = {
        "source": DataSource.dem_glo30,
        "product_id": "Copernicus_DSM_COG_10_N30_00_E079_00_DEM",
        "licence": "Copernicus DEM licence",
    }
    base.update(kw)
    return ProductRecord(**base)  # type: ignore[arg-type]


def test_request_validates_bbox_and_time_order() -> None:
    req = IngestRequest(aoi_id="chamoli-rishiganga", bbox_4326=BBOX, time_start=T0, time_end=T1)
    assert req.params == {}
    with pytest.raises(ValueError, match="bbox_4326"):
        IngestRequest(aoi_id="x", bbox_4326=(80.0, 30.0, 79.0, 31.0))
    with pytest.raises(ValueError, match="time_end"):
        IngestRequest(aoi_id="x", bbox_4326=BBOX, time_start=T1, time_end=T0)
    with pytest.raises(ValueError):
        IngestRequest(aoi_id="", bbox_4326=BBOX)


def test_request_is_frozen_and_rejects_unknown_fields() -> None:
    req = IngestRequest(aoi_id="x", bbox_4326=BBOX)
    with pytest.raises(ValueError):
        req.aoi_id = "y"  # type: ignore[misc]
    with pytest.raises(ValueError):
        IngestRequest(aoi_id="x", bbox_4326=BBOX, bogus=1)  # type: ignore[call-arg]


def test_product_record_validation() -> None:
    p = _product(bbox_4326=BBOX, time_start=T0, time_end=T0, estimated_bytes=0)
    assert p.assets == {} and p.properties == {}
    with pytest.raises(ValueError, match="bbox_4326"):
        _product(bbox_4326=(0.0, 5.0, 1.0, 4.0))
    with pytest.raises(ValueError):
        _product(estimated_bytes=-1)
    with pytest.raises(ValueError, match="time_end"):
        _product(time_start=T1, time_end=T0)


def test_credential_spec_needs_env_vars() -> None:
    spec = CredentialSpec(
        name="Earthdata Login",
        env_vars=("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"),
        purpose="ASF downloads",
    )
    assert spec.docs == "docs/CREDENTIALS.md"
    with pytest.raises(ValueError):
        CredentialSpec(name="x", env_vars=(), purpose="y")


def test_dry_run_plan_fetchable_and_roundtrip() -> None:
    req = IngestRequest(aoi_id="x", bbox_4326=BBOX)
    plan = DryRunPlan(
        source=DataSource.dem_glo30,
        adapter="dem_glo30",
        adapter_version="0.1.0",
        request=req,
        products=[_product(estimated_bytes=10)],
        estimated_bytes=10,
        estimate_basis="test",
    )
    assert plan.fetchable
    again = DryRunPlan.model_validate_json(plan.model_dump_json())
    assert again == plan
    refused = plan.model_copy(update={"refusals": ["mixed product levels"]})
    assert not refused.fetchable
    empty = plan.model_copy(update={"products": []})
    assert not empty.fetchable
    unknown = plan.model_copy(update={"estimated_bytes": None})
    assert unknown.estimated_bytes is None  # null, never a guess


def test_port_is_abstract() -> None:
    with pytest.raises(TypeError):
        IngestAdapter()  # type: ignore[abstract]
