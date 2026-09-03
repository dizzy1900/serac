"""Region labels used to stratify the discriminator's splits.

**These are not authoritative boundaries.** They are hand-drawn rectangles, chosen by this
project, whose only job is to give the leave-one-region-out evaluation a defensible grouping
and to let the model card report counts per region. They do not follow tectonic, physiographic
or political boundaries, they are not a published regionalisation, and no scientific claim
rests on their placement. The committed GeoJSON says all of that in its own `properties`, so
the statement travels with the file rather than living only here.

Why regions at all: a random split leaks. Mass-movement seismograms from one mountain range
share path effects, station sets and site responses, so a model split at random can score well
by recognising the corridor rather than the source physics. Leave-one-region-out with High
Mountain Asia held out is the honest test, and because Chamoli 2021 and Langtang 2026 both sit
in HMA, that fold *is* their evaluation.

Boxes are tested in the listed order and the first containing box wins, so the narrow ranges
(European Alps, Caucasus) are placed before the wide ones (Eurasia-scale catch-alls). Anything
matching no box is `other`, which is a real label reported as such, never silently merged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, NamedTuple

REGIONS_GEOJSON: Final = Path("data/regions/discriminator_regions.geojson")

ARTEFACT_NOTE: Final = (
    "serac project artefact for model-evaluation stratification only. Hand-drawn rectangles "
    "chosen by this project; NOT an authoritative, published, tectonic, physiographic or "
    "political boundary. Do not cite or reuse as a regionalisation."
)

OTHER: Final = "other"


class RegionBox(NamedTuple):
    """One named rectangle in EPSG:4326 degrees, (west, south, east, north)."""

    region_id: str
    label: str
    west: float
    south: float
    east: float
    north: float

    def contains(self, latitude: float, longitude: float) -> bool:
        lon = longitude
        if self.west > self.east:  # box crosses the antimeridian
            in_lon = lon >= self.west or lon <= self.east
        else:
            in_lon = self.west <= lon <= self.east
        return in_lon and self.south <= latitude <= self.north


# Ordered: first match wins. Narrow boxes precede the wide catch-alls they sit inside.
REGION_BOXES: Final[tuple[RegionBox, ...]] = (
    RegionBox("european_alps", "European Alps", 4.0, 43.0, 17.5, 48.5),
    RegionBox("caucasus", "Caucasus", 37.0, 38.0, 50.0, 45.5),
    RegionBox("new_zealand", "New Zealand", 165.0, -48.0, 179.9, -33.0),
    RegionBox("japan_kuril", "Japan and the Kuril arc", 127.0, 29.0, 160.0, 51.0),
    RegionBox("high_mountain_asia", "High Mountain Asia", 65.0, 25.0, 105.0, 45.0),
    RegionBox("alaska_yukon", "Alaska and Yukon", -170.0, 54.0, -125.0, 72.0),
    RegionBox("north_american_cordillera", "North American Cordillera", -145.0, 30.0, -100.0, 54.0),
    RegionBox("andes", "Andes", -82.0, -56.0, -60.0, 13.0),
    RegionBox("scandinavia_iceland", "Scandinavia and Iceland", -25.0, 58.0, 33.0, 72.0),
    RegionBox("mediterranean_anatolia", "Mediterranean and Anatolia", -10.0, 30.0, 45.0, 43.0),
    RegionBox("eastern_north_america", "Eastern North America", -100.0, 24.0, -52.0, 54.0),
)

REGION_IDS: Final[tuple[str, ...]] = (*(b.region_id for b in REGION_BOXES), OTHER)

# The held-out fold for leave-one-region-out. Chamoli 2021 and Langtang 2026 are both here,
# which is precisely why this fold is the headline evaluation.
HELD_OUT_REGION: Final = "high_mountain_asia"


def region_for(latitude: float, longitude: float) -> str:
    """The first containing box's id, or `other`."""
    for box in REGION_BOXES:
        if box.contains(latitude, longitude):
            return box.region_id
    return OTHER


def region_label(region_id: str) -> str:
    for box in REGION_BOXES:
        if box.region_id == region_id:
            return box.label
    return "Other / unassigned"


def _feature(box: RegionBox) -> dict[str, object]:
    ring = [
        [box.west, box.south],
        [box.east, box.south],
        [box.east, box.north],
        [box.west, box.north],
        [box.west, box.south],
    ]
    return {
        "type": "Feature",
        "properties": {
            "region_id": box.region_id,
            "label": box.label,
            "priority": REGION_BOXES.index(box),
            "geometry_quality": "hand_digitised_approximate",
            "authoritative": False,
            "purpose": "model_evaluation_stratification",
            "note": ARTEFACT_NOTE,
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def regions_geojson() -> dict[str, object]:
    """The committed artefact, built from `REGION_BOXES` so file and code cannot drift."""
    return {
        "type": "FeatureCollection",
        "name": "serac_discriminator_regions",
        "note": ARTEFACT_NOTE,
        "held_out_region": HELD_OUT_REGION,
        "matching_rule": (
            "Boxes are tested in ascending `priority` and the first containing box wins; a "
            "point in no box is labelled `other`."
        ),
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [_feature(b) for b in REGION_BOXES],
    }


def write_regions_geojson(repo_root: Path) -> Path:
    """(Re)write the committed GeoJSON. A test asserts the file matches this function."""
    path = repo_root / REGIONS_GEOJSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(regions_geojson(), indent=2) + "\n", encoding="utf-8")
    return path
