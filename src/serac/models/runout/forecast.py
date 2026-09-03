"""`RunoutSurrogate.predict()` -> a valid `CascadeForecast`.

Every numeric on a `CascadeForecast` is a `Range`, which is exactly why the surrogate has
quantile heads: `low` and `high` are the 5th and 95th percentile the model actually predicted,
and `best` is its median. Nothing here widens, narrows or invents an interval.

`Range.source_refs` must name at least one source. For a model output the "source" is the model
run, never a document, so the refs are the solver-and-surrogate version slug plus the ensemble
design hash. `ForecastModel.provenance` is `surrogate`, and `confidence_tier` is capped at
`low` because the gates in `reports/runout/surrogate_metrics.json` are the only evidence the
model has and it has never been compared to a validated simulator -- r.avaflow cross-validation
is outstanding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import torch
from numpy.typing import NDArray

from serac.domain.common import Range
from serac.domain.forecast import (
    CascadeForecast,
    ConfidenceTier,
    DammingEstimate,
    ForecastModel,
    ModelProvenance,
    TransectArrival,
)
from serac.models.runout.cascade import (
    DAMMING_V0_LABEL,
    Constriction,
    breach_hydrograph,
    damming_index,
    index_to_probability,
)
from serac.models.runout.params import (
    NOT_RAVAFLOW,
    RESOLUTION_LIMITATION,
    SINGLE_PHASE_LIMITATION,
    SOLVER_NAME,
    SOLVER_VERSION,
)
from serac.models.runout.release import (
    RELEASE_AT_REST_ASSUMPTION,
    RELEASE_BAND_ASSUMPTION,
)
from serac.models.runout.surrogate import (
    DEPTH_THRESHOLD_M,
    SURROGATE_VERSION,
    CorridorFNO,
    Standardiser,
    TransectRegressor,
    parameter_vector,
)
from serac.models.runout.terrain import CONDITIONING_ASSUMPTION, ERODIBLE_ASSUMPTION

F32 = NDArray[np.float32]

MODEL_SOURCE_REF = "serac-swe-voellmy-surrogate-v0"
"""The `Range.source_refs` entry naming the model run rather than a document."""

FORECAST_ASSUMPTIONS: tuple[str, ...] = (
    NOT_RAVAFLOW,
    SINGLE_PHASE_LIMITATION,
    RESOLUTION_LIMITATION,
    CONDITIONING_ASSUMPTION,
    ERODIBLE_ASSUMPTION,
    RELEASE_AT_REST_ASSUMPTION,
    RELEASE_BAND_ASSUMPTION,
    (
        "Intervals are the 5th and 95th percentile heads of the surrogate, trained with the "
        "pinball loss on the frozen ensemble. They are the model's own spread over that "
        "ensemble and do not include structural error in the solver, the DEM or the corridor "
        "geometry."
    ),
    (
        f"Damming numbers are {DAMMING_V0_LABEL}: a dimensionless deposit-to-channel-depth "
        "index mapped through a stated, uncalibrated logistic. They are not a probability "
        "derived from an inventory of landslide dams, because no such inventory exists for "
        "this corridor."
    ),
)


def _range(low: float, best: float, high: float, unit: str, *, notes: str | None = None) -> Range:
    """A `Range` from three quantiles, ordered defensively so a crossing cannot construct one.

    Refuses non-finite input rather than letting a NaN reach the contract: a NaN here means an
    upstream quantity was never measured, and the right response is to fail loudly at the point
    the number was invented, not to publish it.
    """
    if not all(math.isfinite(v) for v in (low, best, high)):
        raise ValueError(
            f"refusing to build a {unit} Range from non-finite quantiles "
            f"(low={low}, best={best}, high={high})"
        )
    lo, hi = (low, high) if low <= high else (high, low)
    mid = min(max(best, lo), hi)
    return Range(
        low=float(lo),
        high=float(hi),
        best=float(mid),
        unit=unit,
        source_refs=[MODEL_SOURCE_REF],
        notes=notes,
    )


@dataclass(frozen=True)
class SurrogatePrediction:
    """The raw arrays behind a forecast, kept so a caller can inspect what fed the `Range`s."""

    chainage_m: NDArray[np.float64]
    max_depth_q: F32
    arrival_q: F32
    reach_probability: F32
    transect_arrival_q: F32
    transect_stage_q: F32
    transect_reach_probability: F32
    transect_ids: list[str]
    latency_s: float


class RunoutSurrogate:
    """Loads a trained checkpoint and turns a parameter vector into a `CascadeForecast`."""

    name = f"{SOLVER_NAME}-surrogate"
    version = SURROGATE_VERSION

    def __init__(
        self,
        fno: CorridorFNO,
        regressor: TransectRegressor,
        standardiser: Standardiser,
        static: F32,
        *,
        depth_scale: float,
        arrival_scale: float,
        transect_ids: list[str],
        chainage_m: NDArray[np.float64],
        bed_min_m: NDArray[np.float64],
        design_hash: str = "",
        device: str = "cpu",
    ) -> None:
        self.fno = fno.eval()
        self.regressor = regressor.eval()
        self.standardiser = standardiser
        self.static = static
        self.depth_scale = depth_scale
        self.arrival_scale = arrival_scale
        self.transect_ids = transect_ids
        self.chainage_m = chainage_m
        self.bed_min_m = bed_min_m
        self.design_hash = design_hash
        self.device = torch.device(device)
        self._static_t = torch.as_tensor(static, device=self.device)

    @classmethod
    def load(
        cls,
        checkpoint: Path,
        *,
        chainage_m: NDArray[np.float64],
        bed_min_m: NDArray[np.float64],
        design_hash: str = "",
        device: str = "cpu",
    ) -> RunoutSurrogate:
        blob = torch.load(checkpoint, map_location=device, weights_only=False)
        config = blob["fno_config"]
        fno = CorridorFNO(n_parameters=config["n_parameters"], n_static=config["n_static"])
        fno.load_state_dict(blob["fno"])
        regressor = TransectRegressor(
            n_parameters=config["n_parameters"], n_transects=len(blob["transect_ids"])
        )
        regressor.load_state_dict(blob["regressor"])
        return cls(
            fno,
            regressor,
            Standardiser(**blob["standardiser"]),
            np.asarray(blob["static"], dtype=np.float32),
            depth_scale=float(blob["depth_scale"]),
            arrival_scale=float(blob["arrival_scale"]),
            transect_ids=list(blob["transect_ids"]),
            chainage_m=chainage_m,
            bed_min_m=bed_min_m,
            design_hash=design_hash,
            device=device,
        )

    # -- inference -------------------------------------------------------------------------------

    def infer(self, parameters: dict[str, Any], resolution_m: float = 30.0) -> SurrogatePrediction:
        import time as _time

        vector = parameter_vector(parameters, resolution_m)[None, :]
        tensor = torch.as_tensor(self.standardiser.apply(vector), device=self.device)
        start = _time.perf_counter()
        with torch.no_grad():
            depth_q, arrival_q, reach = self.fno(tensor, self._static_t)
            t_arrival, t_stage, t_reach = self.regressor(tensor)
        latency = _time.perf_counter() - start
        return SurrogatePrediction(
            chainage_m=self.chainage_m,
            max_depth_q=depth_q[0].cpu().numpy() * self.depth_scale,
            arrival_q=arrival_q[0].cpu().numpy() * self.arrival_scale,
            reach_probability=torch.sigmoid(reach)[0].cpu().numpy(),
            transect_arrival_q=t_arrival[0].cpu().numpy() * self.arrival_scale,
            transect_stage_q=t_stage[0].cpu().numpy() * self.depth_scale,
            transect_reach_probability=torch.sigmoid(t_reach)[0].cpu().numpy(),
            transect_ids=self.transect_ids,
            latency_s=latency,
        )

    # -- the contract ----------------------------------------------------------------------------

    def predict(
        self,
        parameters: dict[str, Any],
        *,
        forecast_id: str,
        aoi_id: str = "lhende-khola-trishuli",
        origin_time_utc: datetime,
        issued_utc: datetime | None = None,
        event_id: str | None = None,
        detection_id: str | None = None,
        resolution_m: float = 30.0,
        reach_threshold: float = 0.5,
    ) -> CascadeForecast:
        """A valid `CascadeForecast` with every `Range` taken from the quantile heads."""
        prediction = self.infer(parameters, resolution_m)
        issued = issued_utc or datetime.now(tz=UTC)
        if issued < origin_time_utc:
            issued = origin_time_utc

        volume = float(parameters["release_volume_m3"])
        source_volume = _range(
            volume,
            volume,
            volume,
            "m3",
            notes=(
                "The release volume is an input to the surrogate, not a prediction: it comes "
                "from the caller (in the replay lane, from the LFH mass estimate). The interval "
                "is degenerate here because this model does not estimate volume."
            ),
        )

        runout = self._runout_range(prediction, reach_threshold)
        arrivals = self._transect_arrivals(prediction, reach_threshold)
        damming = self._damming(prediction)

        return CascadeForecast(
            forecast_id=forecast_id,
            aoi_id=aoi_id,
            event_id=event_id,
            detection_id=detection_id,
            issued_utc=issued,
            origin_time_utc=origin_time_utc,
            source_volume_m3=source_volume,
            runout_km=runout,
            transect_arrivals=arrivals,
            damming=damming,
            model=ForecastModel(
                name=self.name,
                version=self.version,
                provenance=ModelProvenance.surrogate,
                run_id=None,
            ),
            confidence_tier=ConfidenceTier.low,
            assumptions=[
                *FORECAST_ASSUMPTIONS,
                (
                    f"Surrogate {self.name} v{self.version} trained on the frozen ensemble of "
                    f"{SOLVER_NAME} v{SOLVER_VERSION}"
                    + (f" (design hash {self.design_hash})" if self.design_hash else "")
                    + "; see reports/runout/surrogate_metrics.json for the measured gates."
                ),
            ],
        )

    def _runout_range(self, prediction: SurrogatePrediction, threshold: float) -> Range:
        """Furthest chainage inundated above 1 m, per quantile."""
        distances: list[float] = []
        for q in range(prediction.max_depth_q.shape[0]):
            wet = (prediction.max_depth_q[q] > DEPTH_THRESHOLD_M) & (
                prediction.reach_probability > threshold
            )
            distances.append(float(prediction.chainage_m[wet].max()) / 1000.0 if wet.any() else 0.0)
        low, best, high = min(distances), distances[1], max(distances)
        return _range(
            low,
            best,
            high,
            "km",
            notes=(
                f"Furthest corridor chainage with modelled depth above {DEPTH_THRESHOLD_M:g} m, "
                "from the 5th / 50th / 95th percentile depth heads."
            ),
        )

    def _transect_arrivals(
        self, prediction: SurrogatePrediction, threshold: float
    ) -> list[TransectArrival]:
        out: list[TransectArrival] = []
        for j, transect_id in enumerate(prediction.transect_ids):
            if prediction.transect_reach_probability[j] <= threshold:
                # the model says the flow does not get here; emitting an arrival time anyway
                # would be a fabricated number, so the transect is simply omitted
                continue
            arrival = prediction.transect_arrival_q[j]
            stage = prediction.transect_stage_q[j]
            out.append(
                TransectArrival(
                    transect_id=transect_id,
                    arrival_time_min=_range(
                        arrival[0] / 60.0,
                        arrival[1] / 60.0,
                        arrival[2] / 60.0,
                        "min",
                        notes=(
                            "Minutes after origin_time_utc. The release is emplaced at rest at "
                            "the channel head, so this is biased late by the unmodelled fall "
                            "from the detachment."
                        ),
                    ),
                    peak_stage_m=_range(stage[0], stage[1], stage[2], "m"),
                    peak_discharge_m3s=None,
                    lead_time_min=None,
                )
            )
        return out

    def _damming(self, prediction: SurrogatePrediction) -> DammingEstimate | None:
        """The single worst v0 damming indicator along the corridor, as a `DammingEstimate`."""
        from serac.models.runout.cascade import find_constrictions

        sites = find_constrictions(prediction.chainage_m, self.bed_min_m, n_sites=12)
        if not sites:
            return None
        best: tuple[float, Constriction, float] | None = None
        for site in sites:
            idx = int(np.argmin(np.abs(prediction.chainage_m - site.chainage_m)))
            deposit = float(prediction.max_depth_q[1][idx])
            ratio = deposit / max(site.channel_depth_m, 1e-6)
            if best is None or ratio > best[0]:
                best = (ratio, site, deposit)
        if best is None:
            return None
        _, site, deposit = best
        low_deposit = float(
            prediction.max_depth_q[0][
                int(np.argmin(np.abs(prediction.chainage_m - site.chainage_m)))
            ]
        )
        high_deposit = float(
            prediction.max_depth_q[2][
                int(np.argmin(np.abs(prediction.chainage_m - site.chainage_m)))
            ]
        )
        indicator = damming_index(site, deposit)
        low = damming_index(site, low_deposit)
        high = damming_index(site, high_deposit)
        hydrograph = breach_hydrograph(indicator)
        probability = Range(
            low=index_to_probability(low.index_low),
            high=index_to_probability(high.index_high),
            best=index_to_probability(indicator.index),
            unit="probability",
            source_refs=[MODEL_SOURCE_REF],
            notes=(
                f"{DAMMING_V0_LABEL}: an uncalibrated logistic of the deposit-to-channel-depth "
                "index, not a probability estimated from an inventory of landslide dams. "
                + RESOLUTION_LIMITATION
            ),
        )
        return DammingEstimate(
            probability=probability,
            dam_location=None,
            dam_height_m=_range(
                low.dam_height_m,
                indicator.dam_height_m,
                high.dam_height_m,
                "m",
                notes=DAMMING_V0_LABEL,
            ),
            lake_volume_m3=_range(
                low.lake_volume_m3,
                indicator.lake_volume_m3,
                high.lake_volume_m3,
                "m3",
                notes=DAMMING_V0_LABEL,
            ),
            breach_time_after_formation_min=_range(
                hydrograph.total_time_s / 60.0 * 0.5,
                hydrograph.total_time_s / 60.0,
                hydrograph.total_time_s / 60.0 * 2.0,
                "min",
                notes=(
                    f"{DAMMING_V0_LABEL}: the duration of a parametric triangular breach wave, "
                    "with a stated factor-of-two band. Not a breach-timing model."
                ),
            ),
        )
