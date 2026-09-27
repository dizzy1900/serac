"""v1 self-supervised autoencoder over the feature cube.

Interface only; no model has been trained in this repository.

The class exists so L1 can grow a reconstruction model without changing the watch port. It
does not load weights, does not train, and does not invent a reconstruction error. An absent
weight file scores as `insufficient_data` / `not_fitted` — never Quiet, never a residual of
0.0 that would look like stability.

`encode` / `decode` type against `AutoencoderBackend` rather than torch so importing this
module does not pull a GPU stack into unit tests. No backend is shipped.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from serac.domain.watch import WatchTier
from serac.domain.watch_model import (
    AutoencoderSpec,
    UnitWatchScore,
    WatchScoreInsufficientReason,
)
from serac.errors import SeracError
from serac.ports.watch import (
    FeatureCubeView,
    WatchAnomalyModel,
    WatchModelInfo,
    causal_max_input_time,
)

FloatArray = NDArray[np.float64]

CUBE_AUTOENCODER_NAME = "cube-autoencoder-v1"
CUBE_AUTOENCODER_VERSION = "0.1.0"
CUBE_AUTOENCODER_NOTES = "interface only; no model has been trained in this repository"


class NotFittedError(SeracError):
    """encode/decode called with no loaded weights. score_unit does not raise this."""


class AutoencoderBackend(Protocol):
    """Latent map a trained model would provide. No implementation is shipped."""

    def encode(self, window: FloatArray) -> FloatArray:
        """`(n_timesteps, n_layers)` causal window → latent code."""

    def decode(self, latent: FloatArray) -> FloatArray:
        """Latent code → reconstructed window, same layout as `encode` input."""


class CubeAutoencoderWatch(WatchAnomalyModel):
    """v1 cube autoencoder behind `WatchAnomalyModel`.

    Interface only; no model has been trained in this repository.

    `__init__` takes an `AutoencoderSpec` and an optional `weights_path`. A path does not
    load a model: no weight format has been defined, and this class will not pretend it has.
    """

    def __init__(
        self,
        spec: AutoencoderSpec | None = None,
        weights_path: str | Path | None = None,
    ) -> None:
        self.spec = spec if spec is not None else AutoencoderSpec.v1_defaults()
        self.weights_path = Path(weights_path) if weights_path is not None else None

    @property
    def required_layers(self) -> tuple[str, ...]:
        return self.spec.layers

    @property
    def is_fitted(self) -> bool:
        """Always False: no weight format exists in this repository."""
        return False

    def info(self) -> WatchModelInfo:
        return WatchModelInfo(
            name=CUBE_AUTOENCODER_NAME,
            version=CUBE_AUTOENCODER_VERSION,
            is_fitted=self.is_fitted,
            required_layers=self.required_layers,
            weights_path=None if self.weights_path is None else str(self.weights_path),
            notes=CUBE_AUTOENCODER_NOTES,
        )

    def encode(self, window: FloatArray) -> FloatArray:
        """Map a causal cube window to the latent space. Raises if no weights."""
        del window
        raise NotFittedError(CUBE_AUTOENCODER_NOTES)

    def decode(self, latent: FloatArray) -> FloatArray:
        """Reconstruct cube layers from a latent code. Raises if no weights."""
        del latent
        raise NotFittedError(CUBE_AUTOENCODER_NOTES)

    def score_unit(
        self, unit_id: str, as_of: datetime, cube_view: FeatureCubeView
    ) -> UnitWatchScore:
        """Refuse to score until weights exist. Does not invent a residual.

        Causality is still reported: `max_input_time` is the latest sample with `t <= as_of`
        among `required_layers`, even though those samples are not reconstructed.
        """
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        max_input_time = causal_max_input_time(cube_view, unit_id, self.required_layers, as_of)
        return UnitWatchScore(
            unit_id=unit_id,
            as_of=as_of,
            score=None,
            tier=WatchTier.insufficient_data,
            insufficient_reason=WatchScoreInsufficientReason.not_fitted,
            model_name=CUBE_AUTOENCODER_NAME,
            model_version=CUBE_AUTOENCODER_VERSION,
            max_input_time=max_input_time,
        )
