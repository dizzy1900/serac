"""Watch-model scoring types: the L1 port's return value, not a failure forecast.

`SlopeWatchState` (`serac.domain.watch`) is the published slope-unit snapshot the backtest
writes. This module is the *model-port* contract: one unit, one `as_of`, scored by any
`WatchAnomalyModel` (v0 robust-z or the v1 cube autoencoder interface).

The score is an anomaly statistic. **It is not a failure date and not a probability of
collapse.** An unfitted or unmeasurable unit is `insufficient_data` (or a null tier) with a
reason; it is never Quiet, which is how an unobservable slope would otherwise look safe.

`AutoencoderSpec` is constructor configuration for the v1 interface, not a bus payload. It is
not in `CONTRACTS`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from serac.domain.common import DOMAIN_CONFIG, SEMVER_PATTERN
from serac.domain.watch import WatchTier

WATCH_ANOMALY_SCORE_CONTRACT_VERSION = "0.1.0"

SELF_SUPERVISED_CUBE_RECONSTRUCTION: Final = (
    "self-supervised reconstruction of cube layers; labels unused"
)
"""The only training protocol the v1 interface may claim. Labels are unused on purpose."""

DEFAULT_AUTOENCODER_LAYERS: Final[tuple[str, ...]] = (
    "s1_coherence_t",
    "s1_los_velocity_t",
    "s2_ndsi_t",
)
"""Feature-cube layers a v1 autoencoder would reconstruct. Not a claim that they were trained."""

MASKED_MSE: Final = "masked_mse"


class WatchScoreInsufficientReason(StrEnum):
    """Why `score_unit` did not assess the unit. A unit that cannot be scored is never Quiet.

    The first five members match `WatchInsufficientReason` / the pre-registered v0 model so a
    wrapper can pass reasons through. The rest are port-level: no weights, missing cube layers,
    or a unit the view does not contain. This enum is *not* a change to `slope-watch-state`.
    """

    outside_footprint = "outside_footprint"
    low_los_sensitivity = "low_los_sensitivity"
    too_few_samples = "too_few_samples"
    low_coherence = "low_coherence"
    too_little_history = "too_little_history"
    not_fitted = "not_fitted"
    missing_layers = "missing_layers"
    unit_not_in_view = "unit_not_in_view"


class AutoencoderSpec(BaseModel):
    """What a cube autoencoder would reconstruct, if one were trained.

    This is an interface description. Nothing in this repository has been fit to these layers.
    """

    model_config = DOMAIN_CONFIG

    latent_dim: int = Field(
        ge=1,
        description="Width of the latent code. Unused until weights exist.",
    )
    layers: tuple[str, ...] = Field(
        min_length=1,
        description="Feature-cube layer names the model reads. Samples with t > as_of are ignored.",
    )
    reconstruction_metric: str = Field(
        min_length=1,
        description="Name of the reconstruction residual (e.g. masked_mse). Not a measured error.",
    )
    training_protocol: Literal["self-supervised reconstruction of cube layers; labels unused"] = (
        SELF_SUPERVISED_CUBE_RECONSTRUCTION
    )

    @classmethod
    def v1_defaults(cls) -> AutoencoderSpec:
        """The v1 hook's declared shape. Declaring it is not training it."""
        return cls(
            latent_dim=16,
            layers=DEFAULT_AUTOENCODER_LAYERS,
            reconstruction_metric=MASKED_MSE,
            training_protocol=SELF_SUPERVISED_CUBE_RECONSTRUCTION,
        )


class UnitWatchScore(BaseModel):
    """One slope unit scored at one `as_of` by a `WatchAnomalyModel`.

    `score` is the implementing model's anomaly statistic (v0: robust z; v1: a reconstruction
    residual, once weights exist). It is not a probability of collapse and not a predicted
    date of collapse. `max_input_time` is the latest sample the model actually used and must
    satisfy `max_input_time <= as_of`, matching `anomaly.py`'s `t <= T` inclusive window.
    """

    model_config = DOMAIN_CONFIG

    contract_version: str = WATCH_ANOMALY_SCORE_CONTRACT_VERSION
    unit_id: str = Field(min_length=1)
    as_of: AwareDatetime
    score: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description=(
            "Anomaly statistic from the implementing model. Null when the unit was not assessed. "
            "NOT a probability of collapse and NOT a predicted date of collapse."
        ),
    )
    tier: WatchTier | None = Field(
        default=None,
        description=(
            "Ordinal watch tier, or null when the model did not assess the unit. "
            "Unmeasurable/unfitted units use insufficient_data (or null), never quiet."
        ),
    )
    insufficient_reason: WatchScoreInsufficientReason | None = None
    model_name: str = Field(min_length=1)
    model_version: str = Field(pattern=SEMVER_PATTERN)
    max_input_time: AwareDatetime | None = Field(
        default=None,
        description=(
            "Latest sample time actually consumed. Inclusive trailing windows allow equality "
            "with as_of (t <= as_of). Null when no causal sample was present."
        ),
    )

    @model_validator(mode="after")
    def _score_is_not_a_claim_and_is_causal(self) -> Self:
        unassessed = self.tier is None or self.tier is WatchTier.insufficient_data
        if unassessed and self.score is not None:
            raise ValueError("an unassessed unit has no score; it was not measured")
        if not unassessed and self.score is None:
            raise ValueError(f"tier={self.tier} requires a score")
        if unassessed and self.insufficient_reason is None:
            raise ValueError("an unassessed unit needs an insufficient_reason, not a bare null")
        if not unassessed and self.insufficient_reason is not None:
            raise ValueError(
                f"tier={self.tier} was assessed, so it cannot carry an insufficient_reason"
            )
        if self.max_input_time is not None and self.max_input_time > self.as_of:
            raise ValueError("max_input_time must be <= as_of (causal window: t <= as_of)")
        return self


CONTRACTS: dict[str, type[BaseModel]] = {"watch-anomaly-score": UnitWatchScore}
