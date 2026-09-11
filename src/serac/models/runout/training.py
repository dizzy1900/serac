"""Assemble the ensemble into a dataset, train the surrogate, and score it against the gates.

The gates, all of which are reported with their measured value whether or not they pass:

| Metric | Gate |
|---|---|
| median IoU of inundation at 1 m depth, over test members | >= 0.70 |
| arrival-time MAE per transect | <= 90 s |
| peak-stage relative error | reported |
| p95 inference latency | <= 2 s |
| 5-95% interval coverage | 0.85 - 0.95 |

Coverage is the one that keeps the others honest: a model can hit an IoU gate while emitting
intervals that mean nothing, and a `CascadeForecast` built from meaningless intervals is worse
than no forecast at all.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import torch
from numpy.typing import NDArray
from torch import Tensor

from serac.models.runout.conformal import (
    DEFAULT_LEVEL,
    ConformalCalibration,
    ConformalMode,
    apply_correction,
)
from serac.models.runout.conformal import (
    coverage as interval_coverage,
)
from serac.models.runout.conformal import (
    fit as fit_conformal,
)
from serac.models.runout.corridor import TransectChainage, load_frame, transect_chainages
from serac.models.runout.driver import iter_index
from serac.models.runout.params import SOLVER_NAME, SOLVER_VERSION
from serac.models.runout.surrogate import (
    CORRIDOR_BINS,
    DEPTH_THRESHOLD_M,
    PARAMETER_NAMES,
    QUANTILES,
    SURROGATE_VERSION,
    CorridorFNO,
    Dataset,
    SplitAssignment,
    Standardiser,
    TransectRegressor,
    corridor_features,
    parameter_vector,
    pinball_loss,
    split_by_run,
)

F32 = NDArray[np.float32]
METRICS_FILENAME = "surrogate_metrics.json"
CHECKPOINT_FILENAME = "surrogate.pt"

IOU_GATE = 0.70
ARRIVAL_MAE_GATE_S = 90.0
LATENCY_GATE_S = 2.0
COVERAGE_TARGET = (0.85, 0.95)


def build_dataset(
    repo: Path,
    index_path: Path,
    transects: list[TransectChainage],
    *,
    valid_only: bool = True,
) -> Dataset:
    """Read every member's `corridor.parquet` into stacked arrays."""
    run_ids: list[str] = []
    params: list[F32] = []
    depths: list[F32] = []
    arrivals: list[F32] = []
    reached: list[F32] = []
    t_arrival: list[F32] = []
    t_stage: list[F32] = []
    static: F32 | None = None

    transect_bins: list[int] | None = None
    for row in iter_index(index_path):
        if valid_only and not row.get("valid", False):
            continue
        directory = (
            repo / row["directory"]
            if not Path(row["directory"]).is_absolute()
            else Path(row["directory"])
        )
        parquet = directory / "corridor.parquet"
        if not parquet.exists():
            continue
        table = pq.read_table(parquet)
        chainage = np.asarray(table["chainage_m"], dtype=np.float64)
        depth = np.asarray(table["max_depth_m"], dtype=np.float32)
        arrival = np.asarray(table["arrival_time_s"], dtype=np.float32)
        bed = np.asarray(table["bed_min_m"], dtype=np.float64)
        if depth.shape[0] != CORRIDOR_BINS:
            continue
        if static is None:
            static = corridor_features(bed, chainage)
            transect_bins = [
                int(np.argmin(np.abs(chainage - t.frame_chainage_m))) for t in transects
            ]
        assert transect_bins is not None
        got = np.isfinite(arrival)
        run_ids.append(row["run_id"])
        params.append(parameter_vector(row["parameters"], float(row["resolution_m"])))
        depths.append(depth)
        arrivals.append(np.where(got, arrival, 0.0).astype(np.float32))
        reached.append(got.astype(np.float32))
        t_arrival.append(np.array([arrival[b] for b in transect_bins], dtype=np.float32))
        t_stage.append(np.array([depth[b] for b in transect_bins], dtype=np.float32))

    if static is None:
        raise ValueError(f"no usable members in {index_path}")
    return Dataset(
        run_ids=run_ids,
        parameters=np.stack(params),
        static=static,
        max_depth=np.stack(depths),
        arrival=np.stack(arrivals),
        reached=np.stack(reached),
        transect_arrival=np.stack(t_arrival),
        transect_peak_stage=np.stack(t_stage),
        transect_ids=[t.transect_id for t in transects],
    )


@dataclass
class TrainedSurrogate:
    """The trained models plus everything needed to reproduce their inputs."""

    fno: CorridorFNO
    regressor: TransectRegressor
    standardiser: Standardiser
    static: F32
    depth_scale: float
    arrival_scale: float
    transect_ids: list[str]
    split: SplitAssignment
    calibration: dict[str, ConformalCalibration] = field(default_factory=dict)
    """Conformal corrections fitted on the **val** split, keyed by target. Empty on a model
    trained before ADR-era calibration existed, which is why every consumer treats a missing
    entry as "no correction" rather than as an error."""

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "surrogate_version": SURROGATE_VERSION,
                "solver": SOLVER_NAME,
                "solver_version": SOLVER_VERSION,
                "fno": self.fno.state_dict(),
                "regressor": self.regressor.state_dict(),
                "standardiser": {"mean": self.standardiser.mean, "std": self.standardiser.std},
                "static": self.static,
                "depth_scale": self.depth_scale,
                "arrival_scale": self.arrival_scale,
                "transect_ids": self.transect_ids,
                "split": self.split.as_dict(),
                "calibration": {target: asdict(cal) for target, cal in self.calibration.items()},
                "fno_config": {
                    "n_parameters": self.fno.n_parameters,
                    "n_static": self.fno.n_static,
                },
            },
            path,
        )


def _clone_state(state: Any) -> dict[str, Any]:
    """A loadable deep copy of a `state_dict`, without the non-tensor `_metadata` entry."""
    return {k: copy.deepcopy(v) for k, v in state.items() if k != "_metadata"}


def train(
    data: Dataset,
    *,
    seed: int = 20260903,
    epochs: int = 300,
    batch_size: int = 16,
    learning_rate: float = 2.0e-3,
    device: str = "cpu",
    progress: bool = False,
) -> TrainedSurrogate:
    """Train both heads on the training split, early-stopping on the validation pinball loss."""
    torch.manual_seed(seed)
    split, idx = split_by_run(data.run_ids, seed=seed)
    if not split.is_disjoint():
        raise ValueError("split is not disjoint by run_id")

    train_set = data.subset(idx["train"])
    val_set = data.subset(idx["val"])
    standardiser = Standardiser.fit(train_set.parameters)
    depth_scale = max(float(np.percentile(train_set.max_depth, 99)), 1.0)
    arrival_scale = max(float(np.percentile(train_set.arrival, 99)), 1.0)

    dev = torch.device(device)
    static_t = torch.as_tensor(data.static, device=dev)

    def tensors(subset: Dataset) -> dict[str, Tensor]:
        return {
            "p": torch.as_tensor(standardiser.apply(subset.parameters), device=dev),
            "d": torch.as_tensor(subset.max_depth / depth_scale, device=dev),
            "a": torch.as_tensor(subset.arrival / arrival_scale, device=dev),
            "r": torch.as_tensor(subset.reached, device=dev),
            "ta": torch.as_tensor(
                np.nan_to_num(subset.transect_arrival, nan=0.0) / arrival_scale, device=dev
            ),
            "ts": torch.as_tensor(subset.transect_peak_stage / depth_scale, device=dev),
            "tr": torch.as_tensor(
                np.isfinite(subset.transect_arrival).astype(np.float32), device=dev
            ),
        }

    tr = tensors(train_set)
    va = tensors(val_set)

    fno = CorridorFNO(n_parameters=len(PARAMETER_NAMES), n_static=data.static.shape[0]).to(dev)
    regressor = TransectRegressor(
        n_parameters=len(PARAMETER_NAMES), n_transects=len(data.transect_ids)
    ).to(dev)
    optimiser = torch.optim.AdamW(
        list(fno.parameters()) + list(regressor.parameters()), lr=learning_rate
    )
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=epochs)
    bce = torch.nn.BCEWithLogitsLoss()

    best = float("inf")
    best_state: dict[str, Any] | None = None
    n = tr["p"].shape[0]
    generator = torch.Generator().manual_seed(seed)
    for epoch in range(epochs):
        fno.train()
        regressor.train()
        order = torch.randperm(n, generator=generator)
        for start in range(0, n, batch_size):
            sel = order[start : start + batch_size]
            depth_q, arrival_q, reach = fno(tr["p"][sel], static_t)
            loss = pinball_loss(depth_q, tr["d"][sel], torch.ones_like(tr["d"][sel]), dim=1)
            loss = loss + pinball_loss(arrival_q, tr["a"][sel], tr["r"][sel], dim=1)
            loss = loss + bce(reach, tr["r"][sel])
            ta_q, ts_q, t_reach = regressor(tr["p"][sel])
            loss = loss + pinball_loss(ta_q, tr["ta"][sel], tr["tr"][sel], dim=-1)
            loss = loss + pinball_loss(ts_q, tr["ts"][sel], torch.ones_like(tr["ts"][sel]), dim=-1)
            loss = loss + bce(t_reach, tr["tr"][sel])
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
        schedule.step()

        fno.eval()
        regressor.eval()
        with torch.no_grad():
            depth_q, arrival_q, reach = fno(va["p"], static_t)
            v = pinball_loss(depth_q, va["d"], torch.ones_like(va["d"]), dim=1)
            v = v + pinball_loss(arrival_q, va["a"], va["r"], dim=1)
            ta_q, ts_q, _ = regressor(va["p"])
            v = v + pinball_loss(ta_q, va["ta"], va["tr"], dim=-1)
            v = v + pinball_loss(ts_q, va["ts"], torch.ones_like(va["ts"]), dim=-1)
        value = float(v)
        if value < best:
            best = value
            # neuralop's state_dict carries a `_metadata` entry that is not a tensor and that
            # `load_state_dict` rejects on the way back in, so deep-copy and drop it
            best_state = {
                "fno": _clone_state(fno.state_dict()),
                "regressor": _clone_state(regressor.state_dict()),
            }
        if progress and epoch % 25 == 0:
            print(f"epoch {epoch:4d} val {value:.5f} best {best:.5f}", flush=True)  # noqa: T201

    if best_state is not None:
        fno.load_state_dict(best_state["fno"])
        regressor.load_state_dict(best_state["regressor"])
    model = TrainedSurrogate(
        fno=fno,
        regressor=regressor,
        standardiser=standardiser,
        static=data.static,
        depth_scale=depth_scale,
        arrival_scale=arrival_scale,
        transect_ids=data.transect_ids,
        split=split,
    )
    # Calibrate before returning, so a saved artefact always carries its correction and no
    # consumer has to remember to fit one. Fitted on val; test is never touched here.
    model.calibration.update(calibrate(model, data))
    return model


def _predict(
    model: TrainedSurrogate, data: Dataset, subset: Dataset, *, device: str = "cpu"
) -> tuple[F32, F32, F32]:
    """``(depth quantiles, arrival quantiles, reach probability)`` for one subset, in real units."""
    dev = torch.device(device)
    static_t = torch.as_tensor(data.static, device=dev)
    params = torch.as_tensor(model.standardiser.apply(subset.parameters), device=dev)
    model.fno.eval()
    with torch.no_grad():
        depth_q, arrival_q, reach_logit = model.fno(params, static_t)
    return (
        depth_q.cpu().numpy() * model.depth_scale,
        arrival_q.cpu().numpy() * model.arrival_scale,
        torch.sigmoid(reach_logit).cpu().numpy(),
    )


def calibrate(
    model: TrainedSurrogate,
    data: Dataset,
    *,
    level: float = DEFAULT_LEVEL,
    device: str = "cpu",
) -> dict[str, ConformalCalibration]:
    """Fit conformal corrections for the depth and arrival intervals on the **val** split.

    The val split, never test. Calibrating on test would make the reported coverage a fitted
    quantity and the gate would be scoring the calibration rather than the model. The split is
    disjoint by ``run_id``, so no simulation contributes to both.

    Only the bins the gate scores are used: wet bins for depth, and reached members for arrival.
    Calibrating on the dry bins would be calibrating on the trivial ones — a 5th-percentile head
    that outputs exactly zero covers them for free, which is why the all-bins coverage figure is
    0.99 and says nothing.
    """
    _, idx = split_by_run(data.run_ids, seed=20260903)
    val = data.subset(idx["val"])
    depth, arrival, _reach = _predict(model, data, val, device=device)

    out: dict[str, ConformalCalibration] = {}
    wet = val.max_depth > DEPTH_THRESHOLD_M
    if wet.any():
        out["max_depth_m"] = fit_conformal(
            depth[:, 0][wet],
            depth[:, 2][wet],
            val.max_depth[wet],
            target="max_depth_m",
            level=level,
            mode=ConformalMode.SCALED,
            non_negative=True,
        )
    reached = val.reached > 0.5
    if reached.any():
        out["arrival_time_s"] = fit_conformal(
            arrival[:, 0][reached],
            arrival[:, 2][reached],
            val.arrival[reached],
            target="arrival_time_s",
            level=level,
            mode=ConformalMode.SCALED,
            non_negative=True,
        )
    return out


def evaluate(model: TrainedSurrogate, data: Dataset, *, device: str = "cpu") -> dict[str, Any]:
    """Score the held-out split against every gate and return the metrics document."""
    _, idx = split_by_run(data.run_ids, seed=20260903)
    test = data.subset(idx["test"])
    dev = torch.device(device)
    static_t = torch.as_tensor(data.static, device=dev)
    params = torch.as_tensor(model.standardiser.apply(test.parameters), device=dev)

    model.fno.eval()
    model.regressor.eval()
    with torch.no_grad():
        depth_q, arrival_q, reach_logit = model.fno(params, static_t)
        ta_q, ts_q, _t_reach = model.regressor(params)
    depth = depth_q.cpu().numpy() * model.depth_scale
    arrival = arrival_q.cpu().numpy() * model.arrival_scale
    reach_p = torch.sigmoid(reach_logit).cpu().numpy()
    t_arrival = ta_q.cpu().numpy() * model.arrival_scale
    t_stage = ts_q.cpu().numpy() * model.depth_scale

    # --- inundation IoU at 1 m, per member -------------------------------------------------
    predicted_wet = (depth[:, 1] > DEPTH_THRESHOLD_M) & (reach_p > 0.5)
    truth_wet = test.max_depth > DEPTH_THRESHOLD_M
    ious: list[float] = []
    for i in range(len(test)):
        union = predicted_wet[i] | truth_wet[i]
        if not union.any():
            continue
        ious.append(float((predicted_wet[i] & truth_wet[i]).sum() / union.sum()))
    median_iou = float(np.median(ious)) if ious else 0.0

    # --- arrival-time MAE per transect ------------------------------------------------------
    per_transect: dict[str, Any] = {}
    for j, name in enumerate(model.transect_ids):
        truth = test.transect_arrival[:, j]
        got = np.isfinite(truth)
        if got.sum() == 0:
            per_transect[name] = {
                "reached_members": 0,
                "arrival_mae_s": None,
                "note": "no test member reached this transect",
            }
            continue
        predicted = t_arrival[got, j, 1]
        mae = float(np.abs(predicted - truth[got]).mean())
        stage_truth = test.transect_peak_stage[got, j]
        stage_pred = t_stage[got, j, 1]
        denom = np.maximum(np.abs(stage_truth), 1e-3)
        per_transect[name] = {
            "reached_members": int(got.sum()),
            "arrival_mae_s": round(mae, 2),
            "arrival_mae_gate_s": ARRIVAL_MAE_GATE_S,
            "arrival_gate_pass": bool(mae <= ARRIVAL_MAE_GATE_S),
            "peak_stage_relative_error": round(
                float(np.abs(stage_pred - stage_truth).mean() / np.abs(stage_truth).mean())
                if np.abs(stage_truth).mean() > 0
                else float("nan"),
                4,
            ),
            "peak_stage_median_relative_error": round(
                float(np.median(np.abs(stage_pred - stage_truth) / denom)), 4
            ),
        }

    # --- 5-95% coverage ----------------------------------------------------------------------
    # Over ALL bins this is dominated by dry ones: most of the corridor has max depth 0, the
    # 5th-percentile head can output exactly 0 there, and those bins are covered trivially. The
    # gated number is therefore coverage over the **wet** bins, where the interval has to do
    # some work; the all-bins figure is reported beside it so the difference stays visible.
    # A quantile head is not a calibrated interval. Where a conformal correction was fitted on
    # the val split it is applied here, and the uncalibrated figure is reported beside it so the
    # size of the correction stays visible rather than being absorbed into a passing gate.
    depth_lo_raw, depth_hi_raw = depth[:, 0], depth[:, 2]
    arrival_lo_raw, arrival_hi_raw = arrival[:, 0], arrival[:, 2]
    depth_cal = model.calibration.get("max_depth_m")
    arrival_cal = model.calibration.get("arrival_time_s")
    depth_lo, depth_hi = (
        apply_correction(
            depth_lo_raw,
            depth_hi_raw,
            depth_cal.correction,
            mode=depth_cal.mode,
            non_negative=depth_cal.non_negative,
        )
        if depth_cal is not None
        else (depth_lo_raw, depth_hi_raw)
    )
    arrival_lo, arrival_hi = (
        apply_correction(
            arrival_lo_raw,
            arrival_hi_raw,
            arrival_cal.correction,
            mode=arrival_cal.mode,
            non_negative=arrival_cal.non_negative,
        )
        if arrival_cal is not None
        else (arrival_lo_raw, arrival_hi_raw)
    )

    inside = (test.max_depth >= depth_lo) & (test.max_depth <= depth_hi)
    depth_cover_all = float(inside.mean())
    wet_truth = test.max_depth > DEPTH_THRESHOLD_M
    depth_cover = float(inside[wet_truth].mean()) if wet_truth.any() else float("nan")
    depth_cover_raw = (
        interval_coverage(
            depth_lo_raw[wet_truth], depth_hi_raw[wet_truth], test.max_depth[wet_truth]
        )
        if wet_truth.any()
        else float("nan")
    )
    reached_mask = test.reached > 0.5
    if reached_mask.any():
        arrival_cover = float(
            ((test.arrival >= arrival_lo) & (test.arrival <= arrival_hi))[reached_mask].mean()
        )
        arrival_cover_raw = interval_coverage(
            arrival_lo_raw[reached_mask], arrival_hi_raw[reached_mask], test.arrival[reached_mask]
        )
    else:
        arrival_cover = float("nan")
        arrival_cover_raw = float("nan")

    # --- latency ------------------------------------------------------------------------------
    latencies: list[float] = []
    single = params[:1]
    for _ in range(30):
        start = time.perf_counter()
        with torch.no_grad():
            model.fno(single, static_t)
            model.regressor(single)
        latencies.append(time.perf_counter() - start)
    p95 = float(np.percentile(latencies, 95))

    arrival_maes = [
        v["arrival_mae_s"] for v in per_transect.values() if v.get("arrival_mae_s") is not None
    ]
    return {
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "surrogate_version": SURROGATE_VERSION,
        "solver": {"name": SOLVER_NAME, "version": SOLVER_VERSION},
        "quantiles": list(QUANTILES),
        "n_members": len(data),
        "split_sizes": {
            "train": len(model.split.train),
            "val": len(model.split.val),
            "test": len(model.split.test),
        },
        "splits_disjoint_by_run_id": model.split.is_disjoint(),
        "inundation": {
            "threshold_m": DEPTH_THRESHOLD_M,
            "median_iou": round(median_iou, 4),
            "gate": IOU_GATE,
            "gate_pass": bool(median_iou >= IOU_GATE),
            "members_scored": len(ious),
            "iou_p25": round(float(np.percentile(ious, 25)), 4) if ious else None,
            "iou_p75": round(float(np.percentile(ious, 75)), 4) if ious else None,
        },
        "transects": per_transect,
        "arrival_mae_worst_s": round(max(arrival_maes), 2) if arrival_maes else None,
        "arrival_gate_pass": bool(arrival_maes and max(arrival_maes) <= ARRIVAL_MAE_GATE_S),
        "coverage": {
            "target": list(COVERAGE_TARGET),
            "note": (
                "The gated depth figure is over bins whose true max depth exceeds "
                f"{DEPTH_THRESHOLD_M:g} m. Over all bins the number is dominated by dry ones, "
                "which the 5th-percentile head covers trivially by outputting zero."
            ),
            "max_depth_5_95": round(depth_cover, 4) if np.isfinite(depth_cover) else None,
            "max_depth_5_95_all_bins": round(depth_cover_all, 4),
            "wet_bins_scored": int(wet_truth.sum()),
            "arrival_5_95": round(arrival_cover, 4) if np.isfinite(arrival_cover) else None,
            # The same two figures before any conformal correction, so the size of the
            # correction is visible and a gate cannot be passed by calibration alone without
            # anyone seeing how much calibration it took.
            "uncalibrated": {
                "max_depth_5_95": (
                    round(depth_cover_raw, 4) if np.isfinite(depth_cover_raw) else None
                ),
                "arrival_5_95": (
                    round(arrival_cover_raw, 4) if np.isfinite(arrival_cover_raw) else None
                ),
            },
            "conformal": {
                target: {
                    "mode": cal.mode.value,
                    "level": cal.level,
                    "correction": round(cal.correction, 6),
                    "fitted_on": "val",
                    "n_calibration": cal.n_calibration,
                    "coverage_on_calibration_split": round(cal.coverage_after, 4),
                }
                for target, cal in sorted(model.calibration.items())
            }
            or None,
            "depth_gate_pass": bool(
                np.isfinite(depth_cover) and COVERAGE_TARGET[0] <= depth_cover <= COVERAGE_TARGET[1]
            ),
            "arrival_gate_pass": bool(
                np.isfinite(arrival_cover)
                and COVERAGE_TARGET[0] <= arrival_cover <= COVERAGE_TARGET[1]
            ),
        },
        "latency": {
            "p95_s": round(p95, 5),
            "median_s": round(float(np.median(latencies)), 5),
            "gate_s": LATENCY_GATE_S,
            "gate_pass": bool(p95 <= LATENCY_GATE_S),
            "device": device,
            "note": "one member, both heads, batch size 1, after warm-up",
        },
        "split": model.split.as_dict(),
    }


def write_metrics(metrics: dict[str, Any], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / METRICS_FILENAME
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def train_and_evaluate(
    repo: Path,
    *,
    aoi_id: str = "lhende-khola-trishuli",
    reports_dir: Path | None = None,
    epochs: int = 300,
    device: str = "cpu",
    progress: bool = False,
) -> tuple[dict[str, Any], Path]:
    """End-to-end: dataset from the ensemble index, train, evaluate, write metrics."""
    reports = reports_dir or (repo / "reports" / "runout")
    index_path = reports / "ensemble_index.jsonl"
    aoi_dir = repo / "data" / "aoi" / aoi_id
    frame = load_frame(aoi_dir, 32645)
    transects = transect_chainages(aoi_dir, frame)
    data = build_dataset(repo, index_path, transects)
    model = train(data, epochs=epochs, device=device, progress=progress)
    model.save(reports / CHECKPOINT_FILENAME)
    metrics = evaluate(model, data, device=device)
    return metrics, write_metrics(metrics, reports)
