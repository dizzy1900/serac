"""The frozen ensemble's design prior, expressed as a `CascadeForecast`. Not a forecast.

`make underwriting-check` is supposed to run the avoided-loss computation for the Lhende AOI
on the Langtang replay. The replay produces no forecast -- M2 refuses, so no release volume
exists -- and inventing one is out of the question. The **best available input** is therefore
the frozen runout ensemble itself: 230 solver runs over a Latin-hypercube design that was
frozen (and hashed) before Langtang was ever compared to anything, and whose arrival
distribution at each transect is pure model output.

Read the object this module builds as: *"if a release drawn from the ensemble's design prior
occurred at the Lhende source zone, this is when the solver says the flow reaches each
transect."* It is a sampling design conditioned on nothing. Accordingly:

* `confidence_tier` is `unqualified` and `ForecastModel.name` is
  `serac-swe-voellmy-ensemble-prior`, so any CAP message built from it is `status: Test`;
* every `Range` carries `best=None` -- a design prior has no central estimate;
* `source_volume_m3` is the **design's** release-volume interval, not an estimate of the
  26 August 2026 release, and the notes say so;
* `peak_stage_m` is absent at every transect, because the committed ensemble artifact records
  arrival times and not stages. That absence is why the loss table comes back undetermined,
  and filling it with a deposit depth measured at a different location would be a
  substitution, not an estimate.

The counterfactual lead time
----------------------------
`TransectArrival.lead_time_min` is populated with a **counterfactual**: the modelled arrival
minus the time an alert would have taken to issue, where that time is assembled from measured
stage numbers (M1's own theoretical floor, M2's measured wall clock, M4's measured surrogate
latency) plus one stated dissemination allowance. serac issued no alert for this event, so
this is not a delivered lead time and is never called one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from serac.cascade.evidence import TransectArrivalStats, surrogate_latency_s
from serac.cascade.exposure import ExposureBundle
from serac.domain.avoided_loss import (
    AvoidedLossRequest,
    ExposureItem,
    InterventionKind,
    WarningScenario,
)
from serac.domain.common import Range
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    ForecastModel,
    ModelProvenance,
    TransectArrival,
)

PRIOR_MODEL_NAME = "serac-swe-voellmy-ensemble-prior"
PRIOR_SOURCE_REF = "serac-runout-ensemble-prior"
PRIOR_VERSION = "0.2.0"

NOT_A_FORECAST = (
    "This is the frozen runout ensemble's design prior, not a forecast of this event. The "
    "release volume, ice fraction, release band and friction parameters are Latin-hypercube "
    "samples from a design frozen before any comparison was made; nothing here is conditioned "
    "on the 26 August 2026 event, because M2 refused and no release volume for it exists."
)

DISSEMINATION_ALLOWANCE_S = 30.0
"""ASSUMPTION: seconds between a CAP message being produced and reaching a recipient who can
act. No dissemination path exists, has been built, or has been timed; 30 s is a stated round
number so the counterfactual has every term visible, not a measurement."""

BASELINE_SCENARIO = "no-warning"
WARNING_SCENARIO = "serac-counterfactual-warning"


@dataclass(frozen=True)
class IssueDelay:
    """How long after origin an alert could have been issued, term by term."""

    detection_s: float
    detection_basis: str
    lfh_s: float | None
    lfh_basis: str
    surrogate_s: float | None
    dissemination_s: float

    @property
    def total_s(self) -> float | None:
        if self.lfh_s is None or self.surrogate_s is None:
            return None
        return self.detection_s + self.lfh_s + self.surrogate_s + self.dissemination_s

    def as_note(self) -> str:
        total = self.total_s
        lfh = None if self.lfh_s is None else round(self.lfh_s, 1)
        surrogate = None if self.surrogate_s is None else round(self.surrogate_s, 3)
        return (
            "COUNTERFACTUAL alert-issue delay after origin: "
            f"detection {self.detection_s:.1f} s ({self.detection_basis}) + "
            f"LFH {lfh} s "
            f"({self.lfh_basis}) + "
            f"surrogate {surrogate} s "
            f"+ dissemination {self.dissemination_s:.0f} s (ASSUMPTION) = "
            f"{'undetermined' if total is None else f'{total:.1f} s'}. serac issued no alert for "
            "this event; this is what the measured stage numbers imply, not a delivered lead time."
        )


def issue_delay(repo: Path, event_id: str) -> IssueDelay:
    """Assemble the counterfactual alert-issue delay from measured component numbers."""
    from serac.models.discriminator.latency import theoretical_floor_s

    detection_s = theoretical_floor_s("sliding_180s")
    detection_basis = (
        "M1's own theoretical floor for the causal sliding_180s mode (travel time to a "
        ">=100 km receiver plus 180 s of record minus the 60 s pre-origin lead-in). The "
        "measured latency is not used because the detector did not fire on this event"
    )
    lfh_path = repo / "reports" / "m2" / f"{event_id}.json"
    lfh_s: float | None = None
    lfh_basis = f"no M2 run at {lfh_path.name}"
    if lfh_path.exists():
        doc = json.loads(lfh_path.read_text(encoding="utf-8"))
        wall = doc.get("wall_clock_s")
        if isinstance(wall, int | float):
            lfh_s = float(wall)
            status = doc.get("force_history", {}).get("status")
            lfh_basis = (
                f"M2's measured wall clock on this event ({status}); a refusal is reached "
                "before the inversion runs, so a run that produced a mass would take longer"
            )
    return IssueDelay(
        detection_s=detection_s,
        detection_basis=detection_basis,
        lfh_s=lfh_s,
        lfh_basis=lfh_basis,
        surrogate_s=surrogate_latency_s(repo),
        dissemination_s=DISSEMINATION_ALLOWANCE_S,
    )


def _prior_range(low: float, high: float, unit: str, notes: str) -> Range:
    lo, hi = (low, high) if low <= high else (high, low)
    return Range(low=lo, high=hi, best=None, unit=unit, source_refs=[PRIOR_SOURCE_REF], notes=notes)


def design_volume_range(repo: Path) -> Range:
    """The frozen design's release-volume interval, read from `ensemble_design.json`."""
    path = repo / "reports" / "runout" / "ensemble_design.json"
    low, high = 0.0, 0.0
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        for dimension in doc.get("dimensions", []):
            if dimension.get("name") == "release_volume_m3":
                low = float(dimension["low"])
                high = float(dimension["high"])
    if high <= 0:
        raise ValueError(f"{path}: no release_volume_m3 dimension in the frozen design")
    return _prior_range(
        low,
        high,
        "m3",
        "The Latin-hypercube DESIGN interval for release volume, sampled log-uniformly. It is "
        "a sampling range, not an estimate of this event's release volume, which is unknown: "
        + NOT_A_FORECAST,
    )


def design_reach_range(repo: Path) -> Range:
    """The ensemble's own min/max reach, from `ensemble_summary.json`."""
    path = repo / "reports" / "runout" / "ensemble_summary.json"
    low, high = 0.0, 0.0
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        reach = doc.get("reach_km", {})
        low = float(reach.get("min", 0.0))
        high = float(reach.get("max", 0.0))
    return _prior_range(
        low,
        high,
        "km",
        "Minimum and maximum modelled reach across the 230 frozen members. " + NOT_A_FORECAST,
    )


def ensemble_prior_forecast(
    repo: Path,
    *,
    aoi_id: str,
    event_id: str,
    stats: list[TransectArrivalStats],
    origin_time_utc: datetime,
    issued_utc: datetime,
) -> CascadeForecast:
    """A `CascadeForecast` carrying the frozen ensemble's arrival prior and nothing else."""
    delay = issue_delay(repo, event_id)
    arrivals: list[TransectArrival] = []
    for stat in stats:
        if not stat.members_reaching or stat.p5_min is None or stat.p95_min is None:
            continue
        arrival = _prior_range(
            stat.p5_min,
            stat.p95_min,
            "min",
            f"5th-95th percentile over the {stat.members_reaching} of {stat.members_total} "
            f"frozen members that reach this transect (median {stat.p50_min:.2f} min). "
            f"{stat.members_total - stat.members_reaching} members do not reach it at all, "
            "which is a model output and not a statement that the transect is safe. "
            + NOT_A_FORECAST,
        )
        lead = None
        if delay.total_s is not None:
            offset = delay.total_s / 60.0
            lead = _prior_range(
                stat.p5_min - offset,
                stat.p95_min - offset,
                "min",
                delay.as_note(),
            )
        arrivals.append(
            TransectArrival(
                transect_id=stat.transect_id,
                arrival_time_min=arrival,
                # Absent on purpose: the committed ensemble artifact records arrival times,
                # not stages. Nothing is substituted for it.
                peak_stage_m=None,
                peak_discharge_m3s=None,
                lead_time_min=lead,
            )
        )
    return CascadeForecast(
        forecast_id=f"ensemble-prior-{aoi_id}",
        aoi_id=aoi_id,
        event_id=event_id,
        detection_id=None,
        issued_utc=max(issued_utc, origin_time_utc),
        origin_time_utc=origin_time_utc,
        source_location=None,
        source_volume_m3=design_volume_range(repo),
        runout_km=design_reach_range(repo),
        footprint=None,
        transect_arrivals=arrivals,
        damming=None,
        model=ForecastModel(
            name=PRIOR_MODEL_NAME,
            version=PRIOR_VERSION,
            provenance=ModelProvenance.simulator,
            run_id=None,
        ),
        confidence_tier=ConfidenceTier.unqualified,
        assumptions=[
            NOT_A_FORECAST,
            "Every Range carries best=null: a design prior has no central estimate.",
            "No footprint polygon: the committed ensemble artifacts record chainage profiles "
            "and transect arrivals, not an inundation polygon.",
            "No peak stage at any transect: see above. The avoided-loss engine therefore "
            "cannot evaluate a damage function and reports every asset as undetermined.",
            delay.as_note(),
        ],
    )


def prior_request(
    forecast: CascadeForecast,
    exposure: ExposureBundle,
    *,
    request_id: str,
    requested_utc: datetime,
    lead_time: Range | None = None,
) -> AvoidedLossRequest:
    """Wrap a forecast and an AOI's exposure into a request with the two standard scenarios."""
    span = _lead_time_span(forecast) if lead_time is None else lead_time
    scenarios = [
        WarningScenario(
            scenario_id=BASELINE_SCENARIO,
            intervention=InterventionKind.none,
            description="no warning is issued: the counterfactual baseline",
            assumptions=[],
        )
    ]
    if span is not None:
        scenarios.append(
            WarningScenario(
                scenario_id=WARNING_SCENARIO,
                intervention=InterventionKind.warning,
                lead_time_min=span,
                description=(
                    "a warning issued at the counterfactual alert-issue time. Per-transect lead "
                    "times on the forecast's arrivals take precedence over this span, which is "
                    "the fallback for an asset whose arrival carries none"
                ),
                assumptions=[
                    "serac issued no alert for this event. This scenario asks what the measured "
                    "stage latencies would have delivered had the chain produced a forecast."
                ],
            )
        )
    return AvoidedLossRequest(
        request_id=request_id,
        requested_utc=requested_utc,
        requester="serac cascade",
        forecast=forecast,
        exposure=_ordered(exposure.items),
        scenarios=scenarios,
    )


def _ordered(items: list[ExposureItem]) -> list[ExposureItem]:
    return sorted(items, key=lambda i: (i.transect_id or "~", i.asset_id))


def _lead_time_span(forecast: CascadeForecast) -> Range | None:
    leads = [a.lead_time_min for a in forecast.transect_arrivals if a.lead_time_min is not None]
    if not leads:
        return None
    return _prior_range(
        min(x.low for x in leads),
        max(x.high for x in leads),
        "min",
        "Span of the counterfactual per-transect lead times across every reached transect.",
    )
