"""Damage functions, replacement values and warning benefit -- every one an assumption.

**No vulnerability curve, asset valuation or evacuation-effectiveness study was fetched for
this corridor, and none is cited here.** The CLAUDE.md citation rule requires a document that
was retrieved and hashed in-session before a number may claim a source; nothing in this module
has one. So every parameter below is declared `provenance="assumption"`, carries the sentence
that goes into `AvoidedLossResponse.assumptions[]`, and is overridable by a caller who has a
real schedule. An operator with insured values and their own depth-damage curves should
replace these wholesale: they are a shape and a scale, not a loss estimate.

What is deliberately *not* here
-------------------------------
There is no "default" flow depth, no "default" replacement value and no "typical" population.
When an input is missing the asset's loss comes back `undetermined` with the missing input
named. A zero would read as "no loss expected", which is a different claim and a dangerous
one -- particularly on this corridor, where the runout surrogate fails to reach three of four
transects for reasons `reports/runout/langtang_sanity.md` measures.

Functional form
---------------
One family for every asset class, so the shape is inspectable in one place:

    fraction(d) = 1 - exp(-(d / d0) ** p),  clamped to [0, 1], fraction(0) = 0

`d` is flow depth at the asset in metres; `d0` is the depth at which the function reaches
1 - 1/e (about 63%) of total loss; `p` controls how abruptly damage sets in. Damage is
monotone in depth and saturates, which is the qualitative behaviour every published
depth-damage relation shares. A *low* loss estimate uses the *large* `d0` and a *high* loss
estimate the *small* one, so the interval brackets the parameter uncertainty rather than
hiding it.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from serac.domain.events import AssetType

CASCADE_LOSS_VERSION = "0.1.0"

ASSUMPTION_MARKER = "ASSUMPTION"
"""Every assumption sentence starts with this so a reader (and a grep) cannot miss them."""

NO_CURVE_FETCHED = (
    f"{ASSUMPTION_MARKER}: serac fetched no depth-damage curve, asset valuation or "
    "evacuation-effectiveness study for the Nepal/Tibet Trishuli corridor. Every damage "
    "function, replacement value and warning-benefit parameter in serac.cascade.damage is a "
    "stated parametric assumption with no cited source, and the monetary outputs inherit that "
    "status. They are not a loss estimate; they are what the stated parameters imply."
)


class ParameterProvenance(StrEnum):
    """Where a parameter came from. `source` requires a fetched, hashed document."""

    assumption = "assumption"
    source = "source"
    caller_supplied = "caller_supplied"


class DamageFunction(BaseModel):
    """A saturating depth-damage relation for one asset class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    applies_to: tuple[AssetType, ...] = Field(min_length=1)
    provenance: ParameterProvenance = ParameterProvenance.assumption
    source_url: str | None = None
    d0_low_damage_m: float = Field(gt=0, description="Large d0: the low end of the loss interval")
    d0_central_m: float = Field(gt=0)
    d0_high_damage_m: float = Field(gt=0, description="Small d0: the high end of the loss interval")
    shape: float = Field(gt=0, description="Exponent p; >1 means damage sets in abruptly")
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered_and_sourced(self) -> Self:
        if not (self.d0_high_damage_m <= self.d0_central_m <= self.d0_low_damage_m):
            raise ValueError(
                f"{self.id}: expected d0_high_damage_m <= d0_central_m <= d0_low_damage_m, got "
                f"{self.d0_high_damage_m}, {self.d0_central_m}, {self.d0_low_damage_m}"
            )
        if self.provenance == ParameterProvenance.source and not self.source_url:
            raise ValueError(f"{self.id}: provenance=source requires source_url")
        if self.provenance != ParameterProvenance.source and self.source_url:
            raise ValueError(f"{self.id}: source_url is only meaningful with provenance=source")
        return self

    def fraction(self, depth_m: float, d0_m: float) -> float:
        """Damage fraction in [0, 1] at `depth_m` for one `d0`."""
        if not math.isfinite(depth_m):
            raise ValueError(f"{self.id}: depth must be finite, got {depth_m}")
        if depth_m <= 0.0:
            return 0.0
        return min(1.0, 1.0 - math.exp(-((depth_m / d0_m) ** self.shape)))

    def interval(self, depth_low_m: float, depth_high_m: float) -> tuple[float, float]:
        """(low, high) damage fraction over a depth interval and the `d0` interval together."""
        low = self.fraction(min(depth_low_m, depth_high_m), self.d0_low_damage_m)
        high = self.fraction(max(depth_low_m, depth_high_m), self.d0_high_damage_m)
        return (min(low, high), max(low, high))

    def central(self, depth_m: float) -> float:
        return self.fraction(depth_m, self.d0_central_m)

    @property
    def assumption(self) -> str:
        """The sentence that goes into `AvoidedLossResponse.assumptions[]`."""
        if self.provenance == ParameterProvenance.source:
            return (
                f"Damage function '{self.id}' for {'/'.join(self.applies_to)}: "
                f"1-exp(-(d/d0)^{self.shape:g}) with d0 in "
                f"[{self.d0_high_damage_m:g}, {self.d0_low_damage_m:g}] m, "
                f"sourced from {self.source_url}."
            )
        return (
            f"{ASSUMPTION_MARKER}: damage function '{self.id}' for "
            f"{'/'.join(self.applies_to)} is 1-exp(-(d/d0)^{self.shape:g}) with d0 in "
            f"[{self.d0_high_damage_m:g}, {self.d0_low_damage_m:g}] m "
            f"(central {self.d0_central_m:g} m). No source: {self.rationale}"
        )


HYDROPOWER_HEADWORKS = DamageFunction(
    id="hydropower-headworks-v0",
    applies_to=(AssetType.hydropower_plant,),
    d0_high_damage_m=1.0,
    d0_central_m=2.5,
    d0_low_damage_m=6.0,
    shape=1.2,
    rationale=(
        "run-of-river headworks (weir, intake, desilting basin) sit in the channel, so they "
        "are loaded by the first metre of a debris-laden flow; the small d0 reflects that, "
        "and the interval is wide because no fragility study for Himalayan run-of-river "
        "intakes was fetched"
    ),
)

HYDROPOWER_POWERHOUSE = DamageFunction(
    id="hydropower-powerhouse-v0",
    applies_to=(AssetType.hydropower_plant,),
    d0_high_damage_m=2.0,
    d0_central_m=5.0,
    d0_low_damage_m=12.0,
    shape=1.5,
    rationale=(
        "powerhouses sit on a terrace above the normal channel and are built as reinforced "
        "concrete, so they tolerate more depth than the headworks before total loss; the "
        "steeper shape reflects the step from 'wet' to 'flooded to the machine hall'"
    ),
)

BRIDGE = DamageFunction(
    id="bridge-v0",
    applies_to=(AssetType.bridge,),
    d0_high_damage_m=1.5,
    d0_central_m=4.0,
    d0_low_damage_m=9.0,
    shape=2.0,
    rationale=(
        "a bridge is largely unaffected until the flow loads the deck or scours a pier, then "
        "fails over a narrow depth band; the shape exponent of 2 encodes that threshold "
        "behaviour. Deck clearance is the physically right variable and is not in the AOI "
        "record for any bridge here, so depth above the channel bed is used instead"
    ),
)

SETTLEMENT = DamageFunction(
    id="settlement-v0",
    applies_to=(AssetType.settlement,),
    d0_high_damage_m=2.0,
    d0_central_m=5.0,
    d0_low_damage_m=10.0,
    shape=1.0,
    rationale=(
        "aggregate building stock, mixed construction; a gentler exponent because a "
        "settlement is a mixture of structures that fail at different depths rather than a "
        "single structure with a single threshold"
    ),
)

BUILT_OTHER = DamageFunction(
    id="built-other-v0",
    applies_to=(AssetType.border_post, AssetType.road, AssetType.other),
    d0_high_damage_m=2.0,
    d0_central_m=5.0,
    d0_low_damage_m=10.0,
    shape=1.0,
    rationale=(
        "a catch-all for built assets with no class-specific reasoning; identical in form to "
        "the settlement function, which is itself an assumption, so this is an assumption "
        "about an assumption and should be replaced before any figure derived from it is used"
    ),
)

DAMAGE_FUNCTIONS: tuple[DamageFunction, ...] = (
    HYDROPOWER_HEADWORKS,
    HYDROPOWER_POWERHOUSE,
    BRIDGE,
    SETTLEMENT,
    BUILT_OTHER,
)

HYDROPOWER_COMPONENT_SHARE: dict[str, float] = {
    HYDROPOWER_HEADWORKS.id: 0.35,
    HYDROPOWER_POWERHOUSE.id: 0.65,
}
"""ASSUMPTION: how a run-of-river plant's replacement value splits between the in-channel
headworks and everything downstream of them (waterway, powerhouse, switchyard). A plant is
not one asset with one exposure, and treating it as one would put the whole capital value at
the depth the intake sees. The split is a stated guess; no cost breakdown was fetched."""


def damage_function_for(asset_type: AssetType) -> tuple[DamageFunction, ...]:
    """Every function that applies to `asset_type`, in the order they are evaluated."""
    return tuple(f for f in DAMAGE_FUNCTIONS if asset_type in f.applies_to)


class ReplacementValueRule(BaseModel):
    """How a replacement value is derived when the caller supplies none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    price_year: int = Field(default=2026, ge=1900, le=2100)
    hydropower_usd_per_mw_low: float = Field(default=1.5e6, gt=0)
    hydropower_usd_per_mw_high: float = Field(default=4.0e6, gt=0)
    provenance: ParameterProvenance = ParameterProvenance.assumption

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.hydropower_usd_per_mw_low > self.hydropower_usd_per_mw_high:
            raise ValueError("hydropower_usd_per_mw_low exceeds hydropower_usd_per_mw_high")
        return self

    @property
    def assumption(self) -> str:
        return (
            f"{ASSUMPTION_MARKER}: where a caller supplies no replacement value, a hydropower "
            f"plant is valued at {self.hydropower_usd_per_mw_low / 1e6:g}-"
            f"{self.hydropower_usd_per_mw_high / 1e6:g} million {self.currency} per installed "
            f"MW ({self.price_year} prices), with no central value because no qualifying "
            "source supports one. Bridges, settlements, border posts and roads get NO derived "
            "value at all: serac holds no asset-specific input for them (no span, no building "
            "count, no population), and a class-average figure applied to an asset serac knows "
            "nothing about would be a fabricated number. Those assets come back as "
            "'undetermined', never as zero."
        )


class WarningBenefit(BaseModel):
    """How much of an asset's loss a warning of a given lead time can avoid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_type: AssetType
    max_avoidable_share_low: float = Field(ge=0, le=1)
    max_avoidable_share_high: float = Field(ge=0, le=1)
    lead_time_threshold_min: float = Field(ge=0, description="Below this, nothing is avoided")
    lead_time_full_min: float = Field(gt=0, description="At and above this, the full share")
    rationale: str = Field(min_length=1)
    provenance: ParameterProvenance = ParameterProvenance.assumption

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_avoidable_share_low > self.max_avoidable_share_high:
            raise ValueError(f"{self.asset_type}: avoidable share low exceeds high")
        if self.lead_time_threshold_min >= self.lead_time_full_min:
            raise ValueError(f"{self.asset_type}: threshold lead time is not below the full one")
        return self

    def _ramp(self, lead_time_min: float) -> float:
        if lead_time_min <= self.lead_time_threshold_min:
            return 0.0
        span = self.lead_time_full_min - self.lead_time_threshold_min
        return min(1.0, (lead_time_min - self.lead_time_threshold_min) / span)

    def avoidable_share(self, lead_time_min: float) -> tuple[float, float]:
        """(low, high) share of this asset's loss that the warning avoids."""
        ramp = self._ramp(lead_time_min)
        return (self.max_avoidable_share_low * ramp, self.max_avoidable_share_high * ramp)

    @property
    def assumption(self) -> str:
        return (
            f"{ASSUMPTION_MARKER}: for {self.asset_type}, a warning avoids between "
            f"{self.max_avoidable_share_low:.0%} and {self.max_avoidable_share_high:.0%} of the "
            f"physical loss, ramping linearly from {self.lead_time_threshold_min:g} min of lead "
            f"time to {self.lead_time_full_min:g} min. {self.rationale} No study of protective "
            "action effectiveness on this corridor was fetched."
        )


WARNING_BENEFITS: dict[AssetType, WarningBenefit] = {
    AssetType.hydropower_plant: WarningBenefit(
        asset_type=AssetType.hydropower_plant,
        max_avoidable_share_low=0.02,
        max_avoidable_share_high=0.15,
        lead_time_threshold_min=10.0,
        lead_time_full_min=60.0,
        rationale=(
            "A warning cannot move a weir, a waterway or a powerhouse. What it can do is trip "
            "the units, close gates, de-energise the switchyard, evacuate the machine hall and "
            "move vehicles and mobile plant, which protects a small share of the capital value "
            "and much of the restart cost. The share is small on purpose."
        ),
    ),
    AssetType.bridge: WarningBenefit(
        asset_type=AssetType.bridge,
        max_avoidable_share_low=0.0,
        max_avoidable_share_high=0.02,
        lead_time_threshold_min=5.0,
        lead_time_full_min=30.0,
        rationale=(
            "A bridge cannot be protected by a warning at all; the only avoidable physical "
            "loss is whatever is standing on it. The life-safety benefit of closing it is real "
            "and is not a monetary saving, so it does not appear in this share."
        ),
    ),
    AssetType.settlement: WarningBenefit(
        asset_type=AssetType.settlement,
        max_avoidable_share_low=0.0,
        max_avoidable_share_high=0.05,
        lead_time_threshold_min=10.0,
        lead_time_full_min=60.0,
        rationale=(
            "Buildings cannot be moved. Movable contents, livestock and vehicles can, and that "
            "is the whole of the monetary benefit. The benefit that matters for a settlement "
            "is lives, which serac cannot count here because no sourced population figure "
            "exists for any settlement in this AOI."
        ),
    ),
    AssetType.border_post: WarningBenefit(
        asset_type=AssetType.border_post,
        max_avoidable_share_low=0.0,
        max_avoidable_share_high=0.05,
        lead_time_threshold_min=10.0,
        lead_time_full_min=60.0,
        rationale="Treated as a settlement; serac holds no asset-specific basis for anything else.",
    ),
    AssetType.road: WarningBenefit(
        asset_type=AssetType.road,
        max_avoidable_share_low=0.0,
        max_avoidable_share_high=0.02,
        lead_time_threshold_min=5.0,
        lead_time_full_min=30.0,
        rationale="Treated as a bridge: the asset itself cannot be protected by a warning.",
    ),
    AssetType.other: WarningBenefit(
        asset_type=AssetType.other,
        max_avoidable_share_low=0.0,
        max_avoidable_share_high=0.05,
        lead_time_threshold_min=10.0,
        lead_time_full_min=60.0,
        rationale="Catch-all; replace before using any figure derived from it.",
    ),
}

LIVES_UNCOUNTABLE = (
    "Lives in the warned zone are reported as null, not zero. Every settlement in "
    "data/aoi/lhende-khola-trishuli/exposed_assets.geojson carries population=null, because no "
    "qualifying source for a resident or transient population was fetched; the same is true of "
    "the border post and the bridge. serac therefore cannot count who is in a warned zone, and "
    "reporting a fatality figure of any kind -- including zero -- would be an invented number."
)


def all_assumptions(rule: ReplacementValueRule) -> list[str]:
    """Every assumption sentence this module contributes, in a stable order."""
    out = [NO_CURVE_FETCHED, rule.assumption, LIVES_UNCOUNTABLE]
    out += [f.assumption for f in DAMAGE_FUNCTIONS]
    out.append(
        f"{ASSUMPTION_MARKER}: a run-of-river hydropower plant's replacement value is split "
        + ", ".join(f"{share:.0%} to {name}" for name, share in HYDROPOWER_COMPONENT_SHARE.items())
        + ". No cost breakdown was fetched."
    )
    out += [WARNING_BENEFITS[t].assumption for t in sorted(WARNING_BENEFITS, key=str)]
    return out
