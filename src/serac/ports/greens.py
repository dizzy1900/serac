"""Port for Green's functions used by the force-history inversion.

Green's functions are *modelled*: physics evaluated from a published 1-D Earth model, not an
observation. serac records them as `provenance: derived` rather than `synthetic`, because
`synthetic` is reserved for a fabricated stand-in for data serac could not obtain (see
`docs/adr/ADR-0016-modelled-greens-functions.md`). They are never published on the bus, where
they would arrive as a `SeismicTrace` and be indistinguishable from a recording.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.ports.ledger import ManifestLedger

GREENS_PORT_VERSION = "0.1.0"


class EarthModel(StrEnum):
    """1-D Earth models offered by the provider; the shortest period is in the name."""

    prem_a_20s = "prem_a_20s"
    ak135f_5s = "ak135f_5s"
    iasp91_2s = "iasp91_2s"


class GreensRequest(BaseModel):
    """One (distance, depth) response to a unit force, in a 1-D model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: EarthModel = EarthModel.prem_a_20s
    distance_deg: float = Field(ge=0, le=180)
    source_depth_m: float = Field(ge=0)
    dt_s: float = Field(default=1.0, gt=0)
    duration_s: float = Field(default=900.0, gt=0)
    units: Literal["displacement"] = "displacement"
    force_basis: Literal["ZNE_unit_newton"] = "ZNE_unit_newton"

    def cache_key(self) -> str:
        return (
            f"{self.model.value}_d{self.distance_deg:.2f}_z{self.source_depth_m:.0f}"
            f"_dt{self.dt_s:g}_T{self.duration_s:g}"
        )


class GreensTrace(BaseModel):
    """Displacement per newton for one (force component, receiver component) pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    force_component: Literal["up", "north", "east"]
    receiver_component: Literal["Z", "R", "T"]
    samples_m_per_n: list[float] = Field(min_length=1)


class GreensSet(BaseModel):
    """Every trace for one request, with the provenance needed to reproduce it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: GreensRequest
    traces: list[GreensTrace] = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_url: str = Field(min_length=1)
    retrieved_at_utc: AwareDatetime | None = None
    cache_key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    modelled: Literal[True] = True


class GreensPlan(BaseModel):
    """What a library build would fetch, before it fetches anything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: list[GreensRequest]
    cached: int = Field(ge=0)
    to_fetch: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    estimate_basis: str = Field(min_length=1)
    provider_url: str = Field(min_length=1)


class GreensLibrary(ABC):
    """A cache of modelled responses, fetched once and reused across grid nodes."""

    @abstractmethod
    def plan(self, requests: Sequence[GreensRequest]) -> GreensPlan:
        """Say what would be fetched; write nothing."""

    @abstractmethod
    def get(self, request: GreensRequest, ledger: ManifestLedger) -> GreensSet:
        """Return the response, fetching and ledgering it when it is not cached."""
