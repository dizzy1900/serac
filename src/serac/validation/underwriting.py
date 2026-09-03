"""`underwriting-check`: prove the avoided-loss contract round-trips, then refuse to fake it.

Builds a minimal, obviously fictional `AvoidedLossRequest`, validates its JSON form against the
committed `contracts/avoided-loss.v0.json` with a Draft 2020-12 validator, does the same for a
`status=not_implemented` response, and reports what passed. The loss computation itself is
Prompt 2 work; nothing here ever produces a `computed` response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from serac.domain.avoided_loss import (
    AvoidedLossRequest,
    AvoidedLossResponse,
    AvoidedLossStatus,
    ExposureItem,
    InterventionKind,
    WarningScenario,
)
from serac.domain.common import Range
from serac.domain.events import AssetType
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    ForecastModel,
    ModelProvenance,
    TransectArrival,
)
from serac.domain.schema_export import contract_filename

NOT_IMPLEMENTED_MESSAGE = "not implemented: Prompt 2"
NOT_IMPLEMENTED_EXIT_CODE = 2
REQUEST_CONTRACT = "avoided-loss"
RESPONSE_CONTRACT = "avoided-loss-response"

FICTIONAL_NOTICE = (
    "FICTIONAL example built in code for the schema round-trip only; "
    "it describes no real event, place or asset"
)

_EXAMPLE_TIME = datetime(2000, 1, 1, tzinfo=UTC)
_EXAMPLE_RUN = "example-stub-run"


def _example_range(low: float, high: float, unit: str) -> Range:
    return Range(low=low, high=high, unit=unit, source_refs=[_EXAMPLE_RUN], notes=FICTIONAL_NOTICE)


def example_request() -> AvoidedLossRequest:
    """A minimal valid request. Every figure is a placeholder, labelled as such."""
    forecast = CascadeForecast(
        forecast_id="example-forecast",
        aoi_id="example-aoi",
        issued_utc=_EXAMPLE_TIME,
        origin_time_utc=_EXAMPLE_TIME,
        source_volume_m3=_example_range(1.0, 2.0, "m3"),
        runout_km=_example_range(1.0, 2.0, "km"),
        transect_arrivals=[
            TransectArrival(
                transect_id="example-transect", arrival_time_min=_example_range(1.0, 2.0, "min")
            )
        ],
        model=ForecastModel(
            name="serac-example-stub",
            version="0",
            provenance=ModelProvenance.stub,
            run_id=_EXAMPLE_RUN,
        ),
        confidence_tier=ConfidenceTier.unqualified,
        assumptions=[FICTIONAL_NOTICE],
    )
    return AvoidedLossRequest(
        request_id="example-request",
        requested_utc=_EXAMPLE_TIME,
        requester="serac underwriting-check",
        forecast=forecast,
        exposure=[
            ExposureItem(
                asset_id="example-asset",
                asset_type=AssetType.other,
                transect_id="example-transect",
            )
        ],
        scenarios=[
            WarningScenario(
                scenario_id="baseline",
                intervention=InterventionKind.none,
                description="no warning issued (baseline)",
                assumptions=[FICTIONAL_NOTICE],
            ),
            WarningScenario(
                scenario_id="warning",
                intervention=InterventionKind.warning,
                lead_time_min=_example_range(1.0, 2.0, "min"),
                description="a warning with placeholder lead time",
                assumptions=[FICTIONAL_NOTICE],
            ),
        ],
    )


def not_implemented_response(request: AvoidedLossRequest) -> AvoidedLossResponse:
    """The only response Prompt 1 may issue."""
    return AvoidedLossResponse(
        request_id=request.request_id,
        status=AvoidedLossStatus.not_implemented,
        computed_utc=datetime.now(tz=UTC),
        assumptions=["avoided-loss computation is not implemented (Prompt 2)"],
        notes=NOT_IMPLEMENTED_MESSAGE,
    )


def load_contract(contracts_dir: Path, name: str) -> dict[str, Any]:
    path = contracts_dir / contract_filename(name)
    if not path.exists():
        raise FileNotFoundError(f"{path}: run `serac schema export` first")
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def schema_errors(schema: dict[str, Any], instance: Any) -> list[str]:
    """Human-readable Draft 2020-12 validation errors (empty when valid)."""
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]


@dataclass
class UnderwritingCheckResult:
    """Outcome of the round-trip. `passed` lists the steps that succeeded."""

    passed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def run_underwriting_check(contracts_dir: Path) -> UnderwritingCheckResult:
    """Round-trip request and response through pydantic and the committed JSON Schemas."""
    result = UnderwritingCheckResult()

    request = example_request()
    result.passed.append("built example AvoidedLossRequest (fictional placeholder data)")
    request_json = request.model_dump(mode="json")
    again = AvoidedLossRequest.model_validate(request_json)
    if again != request:
        result.failures.append("AvoidedLossRequest pydantic round-trip changed the record")
    else:
        result.passed.append("AvoidedLossRequest pydantic JSON round-trip")

    try:
        request_schema = load_contract(contracts_dir, REQUEST_CONTRACT)
    except FileNotFoundError as exc:
        result.failures.append(str(exc))
        return result
    errors = schema_errors(request_schema, request_json)
    if errors:
        result.failures.extend(f"{REQUEST_CONTRACT}: {e}" for e in errors)
    else:
        result.passed.append(
            f"AvoidedLossRequest validates against {contract_filename(REQUEST_CONTRACT)}"
        )

    response = not_implemented_response(request)
    result.passed.append("built AvoidedLossResponse(status=not_implemented)")
    try:
        response_schema = load_contract(contracts_dir, RESPONSE_CONTRACT)
    except FileNotFoundError as exc:
        result.failures.append(str(exc))
        return result
    errors = schema_errors(response_schema, response.model_dump(mode="json"))
    if errors:
        result.failures.extend(f"{RESPONSE_CONTRACT}: {e}" for e in errors)
    else:
        result.passed.append(
            f"AvoidedLossResponse validates against {contract_filename(RESPONSE_CONTRACT)}"
        )
    return result
