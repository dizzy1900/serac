"""Tiny RFC 7946 helpers shared by the AOI pipeline and its validation suite."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

GEOJSON_CRS_NOTE = "urn:ogc:def:crs:OGC:1.3:CRS84"


def feature(geometry: Mapping[str, Any], properties: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": dict(geometry), "properties": dict(properties)}


def feature_collection(features: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [dict(f) for f in features]}


def dump_geojson(path: Path, collection: Mapping[str, Any]) -> None:
    """Write a FeatureCollection with stable formatting (UTF-8, trailing newline)."""
    path.write_text(json.dumps(collection, ensure_ascii=False) + "\n", encoding="utf-8")
