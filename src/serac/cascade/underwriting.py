"""The `underwriting-check` table: the Lhende AOI on the best input serac actually has.

The brief for this target is "run the avoided-loss computation for the Lhende AOI on the
Langtang replay and print the table". The Langtang replay produces no forecast -- M1 does not
fire on the committed fixtures and M2 refuses on station geometry -- so there is no forecast
to run it on and one will not be invented.

What is run instead is the **frozen runout ensemble's design prior**
(`serac.cascade.prior`): 230 solver runs over a Latin-hypercube design hashed before any
comparison, whose per-transect arrival distribution is pure model output. It is not a forecast
of this event and the printed header says so on the line above the first number.

The result, as of this component: **0 of 14 exposed assets can be costed.** Ten sit at
transects no ensemble member reaches; four sit at the one transect that is reached, where the
committed artifacts record an arrival but no flow depth. Every one of them is reported
`undetermined` with the reason, never as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from serac.cascade.compute import CascadeLossResult, compute_avoided_loss
from serac.cascade.damage import ReplacementValueRule
from serac.cascade.evidence import ensemble_arrivals, lfh_outcome
from serac.cascade.exposure import ExposureBundle, load_exposure
from serac.cascade.prior import PRIOR_MODEL_NAME, ensemble_prior_forecast, prior_request
from serac.domain.forecast import CascadeForecast

UNDERWRITING_AOI = "lhende-khola-trishuli"
UNDERWRITING_EVENT = "langtang-lhende-2026"

BEST_AVAILABLE_INPUT = (
    f"BEST AVAILABLE INPUT, NOT A FORECAST. The hazard input is {PRIOR_MODEL_NAME}: the frozen "
    "runout ensemble's own arrival distribution over its Latin-hypercube design prior. serac "
    "produced no forecast for this event because M2 refused the inversion, so there is no "
    "release volume, no source location and no footprint. Nothing was substituted for them."
)


@dataclass(frozen=True)
class UnderwritingTable:
    """Everything `underwriting-check` needs to print, plus what produced it."""

    aoi_id: str
    event_id: str
    forecast: CascadeForecast
    exposure: ExposureBundle
    result: CascadeLossResult

    @property
    def costed(self) -> int:
        return len(self.result.determined_asset_ids)

    @property
    def total(self) -> int:
        return len(self.exposure.items)


def underwriting_table(
    repo: Path,
    *,
    aoi_id: str = UNDERWRITING_AOI,
    event_id: str = UNDERWRITING_EVENT,
    computed_utc: datetime | None = None,
) -> UnderwritingTable:
    """Build the table for one AOI on the best available input, refusing to invent a forecast."""
    stamp = computed_utc or datetime.now(tz=UTC)
    exposure = load_exposure(repo, aoi_id)
    stats, _ = ensemble_arrivals(repo)
    lfh = lfh_outcome(repo, event_id)
    origin = _origin(repo, event_id) or stamp
    forecast = ensemble_prior_forecast(
        repo,
        aoi_id=aoi_id,
        event_id=event_id,
        stats=stats,
        origin_time_utc=origin,
        issued_utc=stamp,
    )
    request = prior_request(
        forecast,
        exposure,
        request_id=f"underwriting-{event_id}",
        requested_utc=stamp,
    )
    result = compute_avoided_loss(
        request,
        capacities=exposure.capacities,
        rule=ReplacementValueRule(),
        computed_utc=stamp,
        extra_assumptions=[
            BEST_AVAILABLE_INPUT,
            f"M2's outcome for this event: {lfh.outcome.value}. {lfh.summary}",
            *exposure.gaps,
        ],
    )
    return UnderwritingTable(
        aoi_id=aoi_id,
        event_id=event_id,
        forecast=forecast,
        exposure=exposure,
        result=result,
    )


def _origin(repo: Path, event_id: str) -> datetime | None:
    from serac.pipelines.replay import load_origin

    return load_origin(repo, event_id).origin_time_utc
