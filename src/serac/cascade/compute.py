"""`AvoidedLossRequest` -> `AvoidedLossResponse`: expected loss with and without warning.

The engine is deliberately boring. For each exposed asset it needs four things, and it names
whichever one is missing rather than substituting a default:

1. the **transect** the asset sits at (`ExposureItem.transect_id`),
2. an **arrival** at that transect in the forecast (`TransectArrival`),
3. a **flow depth** at that arrival (`TransectArrival.peak_stage_m`), and
4. a **replacement value**, either supplied by the caller or derivable from an
   asset-specific input serac actually holds.

Missing any of them makes that asset `undetermined`, with `blocked_by` naming the first thing
that was absent. **An undetermined asset is not a zero.** A zero says "we expect no loss
here", which on this corridor would be a claim about safety that no model output supports:
`reports/runout/langtang_sanity.md` measures that no ensemble member reaches three of the four
Lhende transects, for structural reasons that bias the model against reaching anywhere.

Aggregation across assets sums `low` with `low` and `high` with `high`. That is the
comonotonic bound -- it assumes the parameter errors move together, which is true of them
(they are the same stated assumptions applied to every asset) and gives the widest honest
interval. It is not a convolution of independent uncertainties and does not pretend to be.

Contract note
-------------
`AvoidedLossResponse` (contract 0.0.0) has two statuses, `computed` and `not_implemented`, and
no per-asset breakdown. A run with no usable input is therefore emitted as
`status=not_implemented` with `notes` beginning `INSUFFICIENT INPUT:` and every reason in
`assumptions[]` -- honest under the committed schema, but the wrong word. The per-asset table
lives on `CascadeLossResult` and is written to the sidecar JSON. `docs`/the M5 report carry
the exact contract change (`insufficient_input` status, `by_asset` list) that would let the
response say this in one field instead of two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from serac.cascade.damage import (
    CASCADE_LOSS_VERSION,
    HYDROPOWER_COMPONENT_SHARE,
    LIVES_UNCOUNTABLE,
    WARNING_BENEFITS,
    DamageFunction,
    ParameterProvenance,
    ReplacementValueRule,
    all_assumptions,
    damage_function_for,
)
from serac.domain.avoided_loss import (
    AvoidedLossRequest,
    AvoidedLossResponse,
    AvoidedLossStatus,
    ExposureItem,
    InterventionKind,
    MoneyRange,
    ScenarioLoss,
    WarningScenario,
)
from serac.domain.common import Range
from serac.domain.events import AssetType
from serac.domain.forecast import CascadeForecast, TransectArrival

INSUFFICIENT_INPUT_PREFIX = "INSUFFICIENT INPUT"
"""Written into `AvoidedLossResponse.notes` when the contract's only honest status is
`not_implemented` even though the computation ran. See the module docstring."""


class BlockReason(StrEnum):
    """Why one asset could not be costed. Ordered by how early the chain stops."""

    no_transect = "no_transect"
    no_arrival = "no_arrival"
    no_flow_depth = "no_flow_depth"
    no_replacement_value = "no_replacement_value"


BLOCK_DETAIL: dict[BlockReason, str] = {
    BlockReason.no_transect: (
        "the exposure record names no transect, so no arrival in the forecast can be attached "
        "to this asset"
    ),
    BlockReason.no_arrival: (
        "the forecast carries no arrival at this asset's transect: the model does not reach "
        "it. That is a model output, NOT a statement that the asset is safe"
    ),
    BlockReason.no_flow_depth: (
        "the arrival at this asset's transect carries no peak stage, so there is no depth to "
        "put into a damage function"
    ),
    BlockReason.no_replacement_value: (
        "no replacement value: the caller supplied none and serac holds no asset-specific "
        "input (installed capacity, span, building count) from which to derive one"
    ),
}


class AssetLoss(BaseModel):
    """One asset under one scenario. Everything numeric is an interval or is null."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    asset_type: AssetType
    transect_id: str | None
    scenario_id: str
    determined: bool
    blocked_by: BlockReason | None = None
    blocked_detail: str | None = None
    arrival_time_min: Range | None = None
    lead_time_min: Range | None = None
    flow_depth_m: Range | None = None
    damage_fraction_low: float | None = Field(default=None, ge=0, le=1)
    damage_fraction_high: float | None = Field(default=None, ge=0, le=1)
    replacement_value: MoneyRange | None = None
    expected_loss: MoneyRange | None = None
    avoided_vs_baseline: MoneyRange | None = None
    components: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CascadeLossResult:
    """The full run: the contract response plus the per-asset detail it cannot carry."""

    response: AvoidedLossResponse
    by_asset: list[AssetLoss]
    determined_asset_ids: list[str]
    undetermined: dict[str, BlockReason]
    lives_in_warned_zone: Range | None
    notes: list[str] = field(default_factory=list)

    @property
    def computed(self) -> bool:
        return self.response.status == AvoidedLossStatus.computed


def _money(
    low: float,
    high: float,
    *,
    rule: ReplacementValueRule,
    basis: str,
    best: float | None = None,
) -> MoneyRange:
    lo, hi = (low, high) if low <= high else (high, low)
    return MoneyRange(
        low=max(0.0, lo),
        high=max(0.0, hi),
        best=best,
        currency=rule.currency,
        price_year=rule.price_year,
        basis=basis,
    )


def replacement_value_for(
    item: ExposureItem,
    asset_capacity_mw: Range | None,
    rule: ReplacementValueRule,
) -> MoneyRange | None:
    """The caller's value if given; else derived from an asset-specific input; else None."""
    if item.replacement_value is not None:
        return item.replacement_value
    if item.asset_type != AssetType.hydropower_plant or asset_capacity_mw is None:
        return None
    capacity_low = asset_capacity_mw.low
    capacity_high = asset_capacity_mw.high
    return _money(
        capacity_low * rule.hydropower_usd_per_mw_low,
        capacity_high * rule.hydropower_usd_per_mw_high,
        rule=rule,
        basis=(
            f"derived: installed capacity {capacity_low:g}-{capacity_high:g} MW x "
            f"{rule.hydropower_usd_per_mw_low / 1e6:g}-"
            f"{rule.hydropower_usd_per_mw_high / 1e6:g} million {rule.currency}/MW "
            f"({rule.provenance}; no source, see serac.cascade.damage)"
        ),
    )


def _depth_interval(arrival: TransectArrival) -> Range | None:
    return arrival.peak_stage_m


def _weighted_damage(
    asset_type: AssetType, depth: Range
) -> tuple[float, float, list[tuple[DamageFunction, float]]]:
    """(low, high) damage fraction and the component weights that produced it."""
    functions = damage_function_for(asset_type)
    if not functions:
        raise ValueError(f"no damage function registered for {asset_type}")
    if asset_type == AssetType.hydropower_plant:
        weighted = [(f, HYDROPOWER_COMPONENT_SHARE[f.id]) for f in functions]
    else:
        weighted = [(f, 1.0 / len(functions)) for f in functions]
    low = 0.0
    high = 0.0
    for function, share in weighted:
        f_low, f_high = function.interval(depth.low, depth.high)
        low += share * f_low
        high += share * f_high
    return (min(low, 1.0), min(high, 1.0), weighted)


def _lead_time(arrival: TransectArrival, scenario: WarningScenario) -> Range | None:
    """The scenario's own lead time when it states one, else the forecast's."""
    if scenario.intervention == InterventionKind.none:
        return None
    # The arrival's own lead time wins: it is per-transect and comes from the forecast, where
    # the scenario's is one span across the whole request. The scenario is the fallback.
    return arrival.lead_time_min or scenario.lead_time_min


def _asset_loss(
    item: ExposureItem,
    forecast: CascadeForecast,
    scenario: WarningScenario,
    capacity: Range | None,
    rule: ReplacementValueRule,
) -> AssetLoss:
    base = {
        "asset_id": item.asset_id,
        "asset_type": item.asset_type,
        "transect_id": item.transect_id,
        "scenario_id": scenario.scenario_id,
    }

    def blocked(reason: BlockReason, **extra: object) -> AssetLoss:
        return AssetLoss(
            **base,  # type: ignore[arg-type]
            determined=False,
            blocked_by=reason,
            blocked_detail=BLOCK_DETAIL[reason],
            **extra,  # type: ignore[arg-type]
        )

    if item.transect_id is None:
        return blocked(BlockReason.no_transect)
    arrival = next(
        (a for a in forecast.transect_arrivals if a.transect_id == item.transect_id), None
    )
    if arrival is None:
        return blocked(BlockReason.no_arrival)
    lead = _lead_time(arrival, scenario)
    depth = _depth_interval(arrival)
    if depth is None:
        return blocked(
            BlockReason.no_flow_depth,
            arrival_time_min=arrival.arrival_time_min,
            lead_time_min=lead,
        )
    value = replacement_value_for(item, capacity, rule)
    if value is None:
        return blocked(
            BlockReason.no_replacement_value,
            arrival_time_min=arrival.arrival_time_min,
            lead_time_min=lead,
            flow_depth_m=depth,
        )

    damage_low, damage_high, weighted = _weighted_damage(item.asset_type, depth)
    baseline_low = value.low * damage_low
    baseline_high = value.high * damage_high
    benefit = WARNING_BENEFITS[item.asset_type]
    if lead is None:
        share_low = share_high = 0.0
        basis_tail = "no warning (baseline)"
    else:
        # The pessimistic end of the loss interval pairs with the pessimistic (short) lead
        # time, so the interval never narrows because a warning was assumed to be timely.
        share_low, _ = benefit.avoidable_share(lead.low)
        _, share_high = benefit.avoidable_share(lead.high)
        basis_tail = (
            f"warning with lead time {lead.low:g}-{lead.high:g} min avoids "
            f"{share_low:.1%}-{share_high:.1%} (serac.cascade.damage.WARNING_BENEFITS)"
        )
    loss_low = baseline_low * (1.0 - share_high)
    loss_high = baseline_high * (1.0 - share_low)
    avoided = _money(
        baseline_low - loss_high,
        baseline_high - loss_low,
        rule=rule,
        basis=f"baseline loss minus scenario loss; {basis_tail}",
    )
    return AssetLoss(
        **base,  # type: ignore[arg-type]
        determined=True,
        arrival_time_min=arrival.arrival_time_min,
        lead_time_min=lead,
        flow_depth_m=depth,
        damage_fraction_low=damage_low,
        damage_fraction_high=damage_high,
        replacement_value=value,
        expected_loss=_money(
            loss_low,
            loss_high,
            rule=rule,
            basis=(
                f"replacement value x damage fraction {damage_low:.3f}-{damage_high:.3f} at "
                f"depth {depth.low:g}-{depth.high:g} m; {basis_tail}"
            ),
        ),
        avoided_vs_baseline=avoided if scenario.intervention != InterventionKind.none else None,
        components=[f"{f.id}@{share:.2f}" for f, share in weighted],
    )


def _aggregate(
    losses: list[AssetLoss], scenario: WarningScenario, rule: ReplacementValueRule, total: int
) -> ScenarioLoss:
    determined = [x for x in losses if x.determined and x.expected_loss is not None]
    low = sum(x.expected_loss.low for x in determined if x.expected_loss)
    high = sum(x.expected_loss.high for x in determined if x.expected_loss)
    avoided: MoneyRange | None = None
    if scenario.intervention != InterventionKind.none:
        a_low = sum(x.avoided_vs_baseline.low for x in determined if x.avoided_vs_baseline)
        a_high = sum(x.avoided_vs_baseline.high for x in determined if x.avoided_vs_baseline)
        avoided = _money(
            a_low,
            a_high,
            rule=rule,
            basis=(
                f"sum over the {len(determined)} costed asset(s); "
                f"{total - len(determined)} of {total} asset(s) are undetermined and contribute "
                "nothing, which is a gap in coverage rather than a zero loss"
            ),
        )
    return ScenarioLoss(
        scenario_id=scenario.scenario_id,
        expected_loss=_money(
            low,
            high,
            rule=rule,
            basis=(
                f"comonotonic sum over the {len(determined)} costed asset(s) of "
                f"{total}; the remaining {total - len(determined)} are undetermined (see "
                "by_asset[].blocked_by) and are NOT counted as zero"
            ),
        ),
        expected_fatalities=None,
        avoided_vs_baseline=avoided,
    )


def compute_avoided_loss(
    request: AvoidedLossRequest,
    *,
    capacities: dict[str, Range] | None = None,
    rule: ReplacementValueRule | None = None,
    computed_utc: datetime | None = None,
    extra_assumptions: list[str] | None = None,
) -> CascadeLossResult:
    """Evaluate every scenario against every exposed asset.

    `capacities` maps asset id -> installed capacity, read from the AOI record by the caller;
    the exposure contract has no capacity field, so a derived hydropower value needs it.
    """
    value_rule = rule or ReplacementValueRule()
    capacity_by_id = capacities or {}
    forecast = request.forecast
    by_asset: list[AssetLoss] = []
    losses: list[ScenarioLoss] = []

    for scenario in request.scenarios:
        scenario_losses = [
            _asset_loss(item, forecast, scenario, capacity_by_id.get(item.asset_id), value_rule)
            for item in request.exposure
        ]
        by_asset.extend(scenario_losses)
        losses.append(_aggregate(scenario_losses, scenario, value_rule, len(request.exposure)))

    baseline_id = next(
        s.scenario_id for s in request.scenarios if s.intervention == InterventionKind.none
    )
    determined_ids = sorted(
        {x.asset_id for x in by_asset if x.determined and x.scenario_id == baseline_id}
    )
    undetermined = {
        x.asset_id: x.blocked_by
        for x in by_asset
        if x.scenario_id == baseline_id and not x.determined and x.blocked_by is not None
    }

    assumptions = all_assumptions(value_rule)
    assumptions.extend(forecast.assumptions)
    if extra_assumptions:
        assumptions.extend(extra_assumptions)
    assumptions.append(
        "Aggregation is comonotonic: interval endpoints are summed with endpoints, because "
        "every asset is costed with the same stated parameters and their errors move together."
    )
    if undetermined:
        assumptions.append(
            f"{len(undetermined)} of {len(request.exposure)} exposed asset(s) could not be "
            "costed and are reported as undetermined, not as zero loss: "
            + "; ".join(
                f"{asset_id} ({reason})" for asset_id, reason in sorted(undetermined.items())
            )
        )

    stamp = computed_utc or datetime.now(tz=UTC)
    if not determined_ids:
        response = AvoidedLossResponse(
            request_id=request.request_id,
            status=AvoidedLossStatus.not_implemented,
            computed_utc=stamp,
            # The contract permits a model on a not_implemented response, and naming the
            # hazard input that produced nothing is more use to a reader than omitting it.
            model=forecast.model,
            assumptions=assumptions,
            losses=[],
            notes=(
                f"{INSUFFICIENT_INPUT_PREFIX}: the computation ran and costed 0 of "
                f"{len(request.exposure)} exposed assets. "
                + "; ".join(
                    f"{asset_id}: {BLOCK_DETAIL[reason]}"
                    for asset_id, reason in sorted(undetermined.items())
                )
                + ". Contract 0.0.0 has no 'insufficient_input' status, so this response uses "
                "'not_implemented'; the computation is implemented and produced no numbers "
                "because it was given no usable input."
            ),
        )
        return CascadeLossResult(
            response=response,
            by_asset=by_asset,
            determined_asset_ids=[],
            undetermined=undetermined,
            lives_in_warned_zone=None,
            notes=[LIVES_UNCOUNTABLE],
        )

    response = AvoidedLossResponse(
        request_id=request.request_id,
        status=AvoidedLossStatus.computed,
        computed_utc=stamp,
        model=forecast.model,
        assumptions=assumptions,
        losses=losses,
        notes=(
            f"serac.cascade v{CASCADE_LOSS_VERSION}: costed "
            f"{len(determined_ids)} of {len(request.exposure)} exposed asset(s); "
            f"{len(undetermined)} undetermined (never zero). Expected fatalities and "
            "lives-in-warned-zone are null: " + LIVES_UNCOUNTABLE
        ),
    )
    return CascadeLossResult(
        response=response,
        by_asset=by_asset,
        determined_asset_ids=determined_ids,
        undetermined=undetermined,
        lives_in_warned_zone=None,
        notes=[LIVES_UNCOUNTABLE],
    )


def parameter_provenance_summary() -> dict[str, str]:
    """A flat map of every parameter group to its provenance, for a report header."""
    return {
        "damage_functions": ParameterProvenance.assumption.value,
        "replacement_values": ParameterProvenance.assumption.value,
        "warning_benefit": ParameterProvenance.assumption.value,
        "hazard_input": "from the forecast supplied in the request",
        "exposure": "from data/aoi/<aoi>/exposed_assets.geojson, each feature sourced",
    }
