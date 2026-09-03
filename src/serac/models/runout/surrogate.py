"""The runout surrogate: a 1-D corridor FNO with quantile heads, plus a transect regressor.

Why quantiles and not point predictions
---------------------------------------
`CascadeForecast` is built out of `Range` objects, and a `Range` needs a low and a high. A
surrogate that emits point predictions cannot fill that contract without someone inventing an
interval afterwards, which is precisely the fabricated precision this repository exists to
avoid. So every head predicts the 5th, 50th and 95th percentile directly, trained with the
pinball loss, and `RunoutSurrogate.predict` maps those three numbers onto a `Range` whose
`low`/`high` are the 5th and 95th and whose `best` is the median.

Two models
----------
* **Corridor FNO** (`neuralop.models.FNO`, 1-D): parameter vector broadcast along the corridor,
  concatenated with static corridor features (bed elevation, thalweg slope, channel confinement,
  normalised chainage) -> `max_depth` and `arrival_time` profiles, three quantiles each, plus a
  `reached` logit. The logit matters: most members stop well short of the corridor's end, so
  "did it get here at all" is a separate question from "when", and regressing arrival time over
  unreached bins would train the model on padding.
* **Transect regressor**: a small MLP from the parameter vector to arrival time, peak stage and
  peak discharge at each committed transect, again with quantile heads. It exists because the
  transects are what the alerting lane consumes, and reading them off an interpolated profile
  loses the transect-specific spread.

Splitting is by `run_id`, never by chainage bin: bins within a member are massively correlated,
so a bin-wise split would leak. `SplitAssignment` is written out with the metrics and
`validate-runout` asserts the three sets are disjoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import torch
from numpy.typing import NDArray
from torch import Tensor, nn

F64 = NDArray[np.float64]
F32 = NDArray[np.float32]

QUANTILES: tuple[float, float, float] = (0.05, 0.5, 0.95)
SURROGATE_VERSION = "0.1.0"
CORRIDOR_BINS = 400

PARAMETER_NAMES: tuple[str, ...] = (
    "release_volume_m3",
    "ice_fraction",
    "release_band_low_m",
    "release_band_high_m",
    "entrainment_coefficient",
    "mu",
    "xi_m_s2",
    "resolution_m",
)
LOG_PARAMETERS: frozenset[str] = frozenset(
    {"release_volume_m3", "entrainment_coefficient", "mu", "xi_m_s2"}
)

DEPTH_THRESHOLD_M = 1.0
"""The inundation threshold the IoU gate is stated at."""


# -- feature assembly --------------------------------------------------------------------------


def parameter_vector(parameters: dict[str, Any], resolution_m: float) -> F32:
    """The 8-element input vector, log-transformed where the design sampled log-uniformly."""
    band = parameters["release_elevation_band_m"]
    raw = {
        "release_volume_m3": float(parameters["release_volume_m3"]),
        "ice_fraction": float(parameters["ice_fraction"]),
        "release_band_low_m": float(band[0]),
        "release_band_high_m": float(band[1]),
        "entrainment_coefficient": float(parameters["entrainment_coefficient"]),
        "mu": float(parameters["mu"]),
        "xi_m_s2": float(parameters["xi_m_s2"]),
        "resolution_m": float(resolution_m),
    }
    out = np.empty(len(PARAMETER_NAMES), dtype=np.float32)
    for i, name in enumerate(PARAMETER_NAMES):
        value = raw[name]
        out[i] = math.log(max(value, 1e-12)) if name in LOG_PARAMETERS else value
    return out


@dataclass
class Standardiser:
    """Mean/std standardisation fitted on the training split only."""

    mean: list[float]
    std: list[float]

    @classmethod
    def fit(cls, values: NDArray[np.float32]) -> Standardiser:
        mean = values.mean(axis=0)
        std = values.std(axis=0)
        std = np.where(std < 1e-8, 1.0, std)
        return cls(mean=[float(m) for m in mean], std=[float(s) for s in std])

    def apply(self, values: NDArray[np.float32]) -> NDArray[np.float32]:
        mean = np.asarray(self.mean, dtype=np.float32)
        std = np.asarray(self.std, dtype=np.float32)
        return (values - mean) / std


def corridor_features(bed_min_m: F64, chainage_m: F64) -> F32:
    """Static per-bin features: normalised bed, thalweg slope, confinement, chainage.

    These are the same for every member at a given resolution, but they are what lets the FNO
    place a member's parameters on the actual corridor rather than learning an index-to-value
    lookup.
    """
    bed = np.asarray(bed_min_m, dtype=np.float64)
    finite = np.isfinite(bed)
    filled = np.where(finite, bed, np.nanmax(bed[finite]) if finite.any() else 0.0)
    span = max(float(filled.max() - filled.min()), 1.0)
    normalised = (filled - filled.min()) / span
    spacing = max(float(chainage_m[1] - chainage_m[0]), 1.0)
    slope = -np.gradient(filled, spacing)
    window = max(3, round(1500.0 / spacing))
    padded = np.pad(filled, window, mode="edge")
    shoulder = np.array(
        [padded[i : i + 2 * window + 1].max() for i in range(len(filled))], dtype=np.float64
    )
    confinement = (shoulder - filled) / 100.0
    position = chainage_m / max(float(chainage_m[-1]), 1.0)
    return np.stack(
        [normalised, np.clip(slope, -1.0, 1.0), np.clip(confinement, 0.0, 5.0), position]
    ).astype(np.float32)


@dataclass
class Dataset:
    """Everything the surrogate trains on, already stacked."""

    run_ids: list[str]
    parameters: NDArray[np.float32]
    static: F32
    max_depth: NDArray[np.float32]
    arrival: NDArray[np.float32]
    reached: NDArray[np.float32]
    transect_arrival: NDArray[np.float32]
    transect_peak_stage: NDArray[np.float32]
    transect_ids: list[str]

    def __len__(self) -> int:
        return len(self.run_ids)

    def subset(self, idx: NDArray[np.int64]) -> Dataset:
        return Dataset(
            run_ids=[self.run_ids[i] for i in idx],
            parameters=self.parameters[idx],
            static=self.static,
            max_depth=self.max_depth[idx],
            arrival=self.arrival[idx],
            reached=self.reached[idx],
            transect_arrival=self.transect_arrival[idx],
            transect_peak_stage=self.transect_peak_stage[idx],
            transect_ids=self.transect_ids,
        )


@dataclass
class SplitAssignment:
    """Which `run_id` went where. Written out so disjointness is checkable, not asserted."""

    train: list[str] = field(default_factory=list)
    val: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)

    def is_disjoint(self) -> bool:
        sets = [set(self.train), set(self.val), set(self.test)]
        total = sum(len(s) for s in sets)
        return len(set().union(*sets)) == total

    def as_dict(self) -> dict[str, list[str]]:
        return {"train": self.train, "val": self.val, "test": self.test}


def split_by_run(
    run_ids: list[str], *, seed: int = 20260903, fractions: tuple[float, float] = (0.7, 0.15)
) -> tuple[SplitAssignment, dict[str, NDArray[np.int64]]]:
    """Split **by run_id**, never by chainage bin: bins within a member are not independent."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(run_ids))
    n_train = round(fractions[0] * len(run_ids))
    n_val = round(fractions[1] * len(run_ids))
    idx = {
        "train": order[:n_train].astype(np.int64),
        "val": order[n_train : n_train + n_val].astype(np.int64),
        "test": order[n_train + n_val :].astype(np.int64),
    }
    assignment = SplitAssignment(
        train=[run_ids[i] for i in idx["train"]],
        val=[run_ids[i] for i in idx["val"]],
        test=[run_ids[i] for i in idx["test"]],
    )
    return assignment, idx


# -- models -------------------------------------------------------------------------------------


def _monotone_quantiles(raw: Tensor, dim: int) -> Tensor:
    """Non-negative, non-crossing quantiles whose **lowest** one can be exactly zero.

    The obvious construction, `cumsum(softplus(raw))`, makes every quantile strictly positive.
    That is fatal for a field that is zero over most of its domain: max depth is 0 at every dry
    chainage bin, and a 5th percentile that can never reach 0 leaves those bins outside the
    5-95% interval by construction. Measured before this fix, depth interval coverage was 0.12
    against a 0.85-0.95 target, and no amount of training could have moved it.

    So the lowest quantile is a ReLU -- it can be exactly zero -- and the rest are non-negative
    increments on top of it.
    """
    index: list[slice | int] = [slice(None)] * raw.ndim
    index[dim] = slice(0, 1)
    lowest = torch.nn.functional.relu(raw[tuple(index)])
    index[dim] = slice(1, None)
    increments = torch.nn.functional.softplus(raw[tuple(index)])
    return torch.cat([lowest, lowest + torch.cumsum(increments, dim=dim)], dim=dim)


class CorridorFNO(nn.Module):
    """1-D FNO over the corridor with quantile heads for depth and arrival, plus a reach logit."""

    def __init__(
        self,
        *,
        n_parameters: int,
        n_static: int,
        n_modes: int = 32,
        hidden_channels: int = 48,
        n_layers: int = 4,
    ) -> None:
        super().__init__()
        from neuralop.models import FNO

        self.n_parameters = n_parameters
        self.n_static = n_static
        n_out = 2 * len(QUANTILES) + 1  # depth q's, arrival q's, reach logit
        self.body = FNO(
            n_modes=(n_modes,),
            in_channels=n_parameters + n_static,
            out_channels=n_out,
            hidden_channels=hidden_channels,
            n_layers=n_layers,
        )

    def forward(self, parameters: Tensor, static: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        """`(depth_quantiles, arrival_quantiles, reach_logit)`, each `(batch, ..., bins)`."""
        batch, bins = parameters.shape[0], static.shape[-1]
        broadcast = parameters[:, :, None].expand(batch, self.n_parameters, bins)
        static_b = static[None].expand(batch, self.n_static, bins)
        out = self.body(torch.cat([broadcast, static_b], dim=1))
        nq = len(QUANTILES)
        depth = _monotone_quantiles(out[:, :nq], dim=1)
        arrival = _monotone_quantiles(out[:, nq : 2 * nq], dim=1)
        reach = out[:, 2 * nq]
        return depth, arrival, reach


class TransectRegressor(nn.Module):
    """Parameters -> per-transect arrival time and peak stage, with quantile heads."""

    def __init__(self, *, n_parameters: int, n_transects: int, hidden: int = 128) -> None:
        super().__init__()
        self.n_transects = n_transects
        self.trunk = nn.Sequential(
            nn.Linear(n_parameters, hidden),
            nn.GELU(),
            nn.Linear(hidden, hidden),
            nn.GELU(),
        )
        self.head = nn.Linear(hidden, n_transects * (2 * len(QUANTILES) + 1))

    def forward(self, parameters: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        nq = len(QUANTILES)
        raw = self.head(self.trunk(parameters))
        raw = raw.view(parameters.shape[0], self.n_transects, 2 * nq + 1)
        arrival = _monotone_quantiles(raw[..., :nq], dim=-1)
        stage = _monotone_quantiles(raw[..., nq : 2 * nq], dim=-1)
        reach = raw[..., 2 * nq]
        return arrival, stage, reach


def pinball_loss(prediction: Tensor, target: Tensor, mask: Tensor, dim: int) -> Tensor:
    """Quantile (pinball) loss over `QUANTILES` along `dim`, averaged over masked entries."""
    quantiles = torch.tensor(QUANTILES, dtype=prediction.dtype, device=prediction.device)
    shape = [1] * prediction.ndim
    shape[dim] = len(QUANTILES)
    q = quantiles.view(shape)
    error = target.unsqueeze(dim) - prediction
    loss = torch.maximum(q * error, (q - 1.0) * error)
    m = mask.unsqueeze(dim).expand_as(loss)
    denominator = m.sum().clamp(min=1.0)
    return (loss * m).sum() / denominator
