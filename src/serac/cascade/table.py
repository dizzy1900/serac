"""Render the avoided-loss table, with a header a reader cannot skim past.

Two renderings, one content: `render_loss_table` for a markdown report and
`print_loss_table` for the terminal (`make underwriting-check`). Both begin with an
**INPUT PROVENANCE** block that states, for this run, which inputs are real, which are absent
and whether a validated forecast exists. That block is not decoration: a table of monetary
figures with no header is the single easiest artifact in this repository to quote out of
context.

The per-asset table always shows every exposed asset, including the ones that could not be
costed, with the reason in the last column. An asset that is missing from a loss table reads
as an asset with no loss.
"""

from __future__ import annotations

from serac.cascade.compute import AssetLoss, BlockReason, CascadeLossResult
from serac.cascade.exposure import ExposureBundle
from serac.domain.avoided_loss import AvoidedLossStatus
from serac.domain.common import Range
from serac.domain.forecast import ModelProvenance

NO_VALIDATED_FORECAST = (
    "NO VALIDATED FORECAST EXISTS FOR THIS EVENT. serac has no model validated against events "
    "(RELEASE_STATUS.md), and for this event the chain produced no forecast at all."
)

BLOCK_SHORT: dict[BlockReason, str] = {
    BlockReason.no_transect: "no transect",
    BlockReason.no_arrival: "model does not reach it",
    BlockReason.no_flow_depth: "no flow depth",
    BlockReason.no_replacement_value: "no replacement value",
}


def _money(value: object) -> str:
    if value is None:
        return "—"
    low = getattr(value, "low", None)
    high = getattr(value, "high", None)
    currency = getattr(value, "currency", "")
    if low is None or high is None:
        return "—"
    return f"{low / 1e6:,.2f} to {high / 1e6:,.2f} M {currency}"


def _range(value: Range | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value.low:.{digits}f} to {value.high:.{digits}f} {value.unit}"


def _fraction(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "—"
    return f"{low:.1%} to {high:.1%}"


PRIOR_MODEL_NAME = "serac-swe-voellmy-ensemble-prior"
"""Renaming the prior must not silently drop its disclosure from the header."""


def provenance_header(result: CascadeLossResult, exposure: ExposureBundle) -> list[str]:
    """The block that must precede any number this module prints."""
    response = result.response
    model = response.model
    is_prior = model is not None and model.name == PRIOR_MODEL_NAME
    lines = [
        "=" * 78,
        "INPUT PROVENANCE — read before reading a single figure below",
        "=" * 78,
        f"AOI                : {exposure.aoi_id} ({exposure.aoi_name})",
        "Hazard input       : "
        + (
            f"{model.name} v{model.version}, provenance={model.provenance}"
            if model is not None
            else "none — the chain produced no forecast"
        ),
    ]
    if is_prior:
        lines.append(
            "                     This is the FROZEN ENSEMBLE'S DESIGN PRIOR, not a forecast "
            "of this event."
        )
    if model is not None and model.provenance == ModelProvenance.stub:
        lines.append("                     Stub provenance: the numbers are placeholders.")
    lines += [
        f"Exposure           : REAL — {len(exposure.items)} asset(s) from "
        f"data/aoi/{exposure.aoi_id}/exposed_assets.geojson, each feature sourced",
        f"Transects          : REAL — {len(exposure.transects)} "
        f"({', '.join(exposure.transect_ids)})",
        "Damage functions   : ASSUMPTION — parametric, no cited source (serac.cascade.damage)",
        "Replacement values : ASSUMPTION where derived; ABSENT for every non-hydropower asset",
        "Warning benefit    : ASSUMPTION — stated ramps, no effectiveness study fetched",
        "Lives in warned zone: NULL — no sourced population for any settlement in this AOI",
        "-" * 78,
        NO_VALIDATED_FORECAST,
    ]
    if response.status == AvoidedLossStatus.not_implemented:
        lines.append(f"RESULT: {response.notes or 'no losses computed'}")
    else:
        lines.append(
            f"RESULT: costed {len(result.determined_asset_ids)} of {len(exposure.items)} "
            f"asset(s); {len(result.undetermined)} undetermined (NOT zero)."
        )
    lines += ["Missing from the exposure layer:"]
    lines += [f"  - {gap}" for gap in exposure.gaps]
    lines.append("=" * 78)
    return lines


def _asset_rows(result: CascadeLossResult, scenario_id: str) -> list[AssetLoss]:
    return [a for a in result.by_asset if a.scenario_id == scenario_id]


def render_loss_table(result: CascadeLossResult, exposure: ExposureBundle) -> str:
    """The whole table as markdown: header block, per-asset rows, per-scenario totals."""
    lines = ["```", *provenance_header(result, exposure), "```", ""]
    scenario_ids = sorted({a.scenario_id for a in result.by_asset})
    for scenario_id in scenario_ids:
        rows = _asset_rows(result, scenario_id)
        lines += [
            f"### Scenario `{scenario_id}`",
            "",
            "| Asset | Type | Transect | Arrival | Lead time | Depth | Damage | Replacement "
            "| Expected loss | Avoided | Status |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            reason = BLOCK_SHORT.get(row.blocked_by, "?") if row.blocked_by else "?"
            status = "costed" if row.determined else f"**undetermined** - {reason}"
            lines.append(
                f"| `{row.asset_id}` | {row.asset_type} | "
                f"{('`' + row.transect_id + '`') if row.transect_id else '—'} | "
                f"{_range(row.arrival_time_min)} | {_range(row.lead_time_min)} | "
                f"{_range(row.flow_depth_m, 2)} | "
                f"{_fraction(row.damage_fraction_low, row.damage_fraction_high)} | "
                f"{_money(row.replacement_value)} | {_money(row.expected_loss)} | "
                f"{_money(row.avoided_vs_baseline)} | {status} |"
            )
        lines.append("")
    if result.response.losses:
        lines += [
            "### Totals",
            "",
            "| Scenario | Expected loss | Avoided vs baseline | Expected fatalities |",
            "|---|---|---|---|",
        ]
        for loss in result.response.losses:
            fatalities = (
                "null" if loss.expected_fatalities is None else _range(loss.expected_fatalities)
            )
            lines.append(
                f"| `{loss.scenario_id}` | {_money(loss.expected_loss)} | "
                f"{_money(loss.avoided_vs_baseline)} | "
                f"{fatalities} |"
            )
        lines.append("")
        lines.append(
            "> Totals are comonotonic sums over the costed assets only. The undetermined "
            "assets are a gap in coverage, not a zero contribution."
        )
        lines.append("")
    lines += ["### Assumptions", ""]
    lines += [f"{n}. {a}" for n, a in enumerate(result.response.assumptions, start=1)]
    lines.append("")
    return "\n".join(lines)


def print_loss_table(result: CascadeLossResult, exposure: ExposureBundle) -> list[str]:
    """Plain-text lines for the terminal. Returns them so a caller can also capture them."""
    out = list(provenance_header(result, exposure))
    scenario_ids = sorted({a.scenario_id for a in result.by_asset})
    width = max((len(a.asset_id) for a in result.by_asset), default=10)
    for scenario_id in scenario_ids:
        out += ["", f"SCENARIO: {scenario_id}", "-" * 78]
        out.append(
            f"{'asset'.ljust(width)}  {'transect':<22} {'arrival(min)':>14} "
            f"{'lead(min)':>14}  status"
        )
        for row in _asset_rows(result, scenario_id):
            reason = BLOCK_SHORT.get(row.blocked_by, "?") if row.blocked_by else "?"
            status = (
                f"costed  loss {_money(row.expected_loss)}"
                if row.determined
                else f"UNDETERMINED  {reason}"
            )
            out.append(
                f"{row.asset_id.ljust(width)}  {(row.transect_id or '—'):<22} "
                f"{_range(row.arrival_time_min):>14} {_range(row.lead_time_min):>14}  {status}"
            )
    if result.response.losses:
        out += ["", "TOTALS", "-" * 78]
        for loss in result.response.losses:
            out.append(
                f"  {loss.scenario_id:<30} loss {_money(loss.expected_loss):>26}  "
                f"avoided {_money(loss.avoided_vs_baseline)}"
            )
        out.append("  expected fatalities: null (no sourced population; see assumptions)")
    out += ["", f"ASSUMPTIONS ({len(result.response.assumptions)}):"]
    out += [f"  {n:2d}. {a}" for n, a in enumerate(result.response.assumptions, start=1)]
    return out
