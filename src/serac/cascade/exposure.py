"""Turn an AOI's committed exposure layer into the `ExposureItem` list the contract wants.

`ExposureItem` is deliberately thin -- id, type, transect, replacement value, population --
so the loss layer never sees geometry it might be tempted to reason about. Everything the
engine needs beyond that (installed capacity, in particular) travels separately in
`ExposureBundle.capacities`, keyed by asset id, because the published contract has no field
for it and adding one is not this component's decision to make.

What the AOI actually carries, measured rather than assumed, is recorded in
`ExposureBundle.gaps`: for `lhende-khola-trishuli` every settlement has `population: null` and
no asset has a replacement value, which is why most of the loss table comes back
`undetermined`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from serac.domain.avoided_loss import ExposureItem
from serac.domain.common import Range
from serac.domain.events import AssetType
from serac.domain.geo import AOI, ExposedAsset, Transect
from serac.errors import SeracError


class ExposureError(SeracError):
    """An AOI's exposure layer could not be read."""


@dataclass(frozen=True)
class ExposureBundle:
    """One AOI's exposure, plus what it is missing."""

    aoi_id: str
    aoi_name: str
    items: list[ExposureItem]
    assets: list[ExposedAsset]
    transects: list[Transect]
    capacities: dict[str, Range]
    gaps: list[str] = field(default_factory=list)

    @property
    def by_id(self) -> dict[str, ExposedAsset]:
        return {a.id: a for a in self.assets}

    @property
    def transect_ids(self) -> list[str]:
        return [t.id for t in self.transects]

    def asset(self, asset_id: str) -> ExposedAsset | None:
        return self.by_id.get(asset_id)


def load_exposure(repo: Path, aoi_id: str) -> ExposureBundle:
    """Read `data/aoi/<aoi_id>/` and build the exposure the loss engine will be given."""
    from serac.pipelines.aoi_build import read_aoi_dir

    directory = repo / "data" / "aoi" / aoi_id
    if not (directory / "aoi.json").exists():
        raise ExposureError(f"{directory}: no AOI there")
    files = read_aoi_dir(directory)
    return bundle_from(files.aoi, files.assets, files.transects)


def bundle_from(aoi: AOI, assets: list[ExposedAsset], transects: list[Transect]) -> ExposureBundle:
    """Build a bundle from already-loaded records (so tests need no AOI directory)."""
    items: list[ExposureItem] = []
    capacities: dict[str, Range] = {}
    for asset in assets:
        items.append(
            ExposureItem(
                asset_id=asset.id,
                asset_type=asset.asset_type,
                transect_id=asset.transect_id,
                # The AOI layer carries no monetary value for any asset. Deriving one here
                # would put a number serac cannot source into the contract, so this stays
                # None and the engine reports the asset as undetermined.
                replacement_value=None,
                population=asset.population,
            )
        )
        if asset.capacity_mw is not None:
            capacities[asset.id] = asset.capacity_mw
    return ExposureBundle(
        aoi_id=aoi.id,
        aoi_name=aoi.name,
        items=items,
        assets=list(assets),
        transects=list(transects),
        capacities=capacities,
        gaps=_gaps(assets),
    )


def _gaps(assets: list[ExposedAsset]) -> list[str]:
    """The measured holes in the exposure layer, stated as facts about the committed data."""
    total = len(assets)
    no_transect = [a.id for a in assets if a.transect_id is None]
    no_value = total  # the AOI layer has no replacement_value field at all
    settlements = [a for a in assets if a.asset_type == AssetType.settlement]
    no_population = [a.id for a in settlements if a.population is None]
    no_capacity = [
        a.id for a in assets if a.asset_type == AssetType.hydropower_plant and a.capacity_mw is None
    ]
    out = [
        f"{no_value} of {total} exposed asset(s) carry no replacement value: the AOI exposure "
        "layer has no monetary field, so a value can only come from the caller or, for "
        "hydropower, be derived from installed capacity under a stated assumption.",
    ]
    if no_population:
        out.append(
            f"{len(no_population)} of {len(settlements)} settlement(s) carry population=null "
            f"({', '.join(no_population)}); no qualifying population source was fetched, so "
            "lives in a warned zone cannot be counted."
        )
    if no_transect:
        out.append(
            f"{len(no_transect)} asset(s) name no transect ({', '.join(no_transect)}), so no "
            "forecast arrival can be attached to them."
        )
    if no_capacity:
        out.append(
            f"{len(no_capacity)} hydropower asset(s) carry no installed capacity "
            f"({', '.join(no_capacity)})."
        )
    return out
