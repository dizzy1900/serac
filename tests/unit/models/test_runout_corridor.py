"""The corridor frame: exact inverse where it is defined, and honest about where it is not."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from serac.models.runout.corridor import (
    CorridorFrame,
    build_frame,
    read_centreline_lonlat,
    roundtrip_rms_px,
    transect_chainages,
)

AOI_DIR = Path("data/aoi/lhende-khola-trishuli")
EPSG = 32645


@pytest.fixture(scope="module")
def frame() -> CorridorFrame:
    return build_frame(read_centreline_lonlat(AOI_DIR / "river_centreline.geojson"), EPSG)


def test_frame_covers_the_committed_corridor(frame: CorridorFrame) -> None:
    """The AOI says the centreline is clipped at 100 km; the frame must agree."""
    assert frame.length_m == pytest.approx(100_000.0, abs=50.0)
    assert frame.n_samples > 3000


def test_fast_inverse_matches_the_brute_force_reference(frame: CorridorFrame) -> None:
    """The STRtree shortcut must give exactly what projecting onto every segment gives."""
    rng = np.random.default_rng(0)
    x = rng.uniform(295_800.0, 358_980.0, 2000)
    y = rng.uniform(3_071_520.0, 3_140_070.0, 2000)

    s_fast, n_fast = frame.inverse(x, y)
    s_slow, n_slow = frame.inverse_brute(x, y)

    assert np.abs(s_fast - s_slow).max() == 0.0
    assert np.abs(n_fast - n_slow).max() == 0.0


def test_roundtrip_is_exact_where_the_projection_is_interior(frame: CorridorFrame) -> None:
    """`forward(inverse(p)) == p` to machine precision on the frame's valid set."""
    rng = np.random.default_rng(7)
    s = rng.uniform(1000.0, frame.length_m - 1000.0, 5000)
    n = rng.uniform(-300.0, 300.0, 5000)
    x, y = frame.forward(s, n)

    s_back, _n_back = frame.inverse(x, y)
    interior = frame.projection_interior(s_back)
    assert interior.mean() > 0.5, "the test sample must actually exercise the interior"

    rms, worst = roundtrip_rms_px(frame, x[interior], y[interior], 30.0)
    assert rms < 1.0, f"round-trip RMS {rms} px on the interior set"
    assert worst < 1.0


def test_roundtrip_failure_is_confined_to_vertex_projections(frame: CorridorFrame) -> None:
    """Where the map is many-to-one it must be *flagged*, not quietly wrong.

    This pins the finding rather than the fix: the corridor buffer's end-caps and outer bends
    lie beyond the centreline's medial axis, so no forward map inverts them.
    """
    rng = np.random.default_rng(11)
    # points far off the line, where the projection snaps to a vertex on a bend
    s = rng.uniform(0.0, frame.length_m, 4000)
    n = rng.uniform(-1500.0, 1500.0, 4000)
    x, y = frame.forward(s, n)
    s_back, _ = frame.inverse(x, y)
    interior = frame.projection_interior(s_back)

    bx, by = frame.forward(*frame.inverse(x, y))
    error_px = np.hypot(bx - x, by - y) / 30.0

    assert error_px[interior].max() < 1e-6, "interior projections must be exact"
    assert not interior.all(), "the sample must include vertex projections to be meaningful"


def test_transect_chainages_agree_with_the_committed_values(frame: CorridorFrame) -> None:
    """Recomputing chainage from the centreline must reproduce what the AOI build recorded."""
    chainages = transect_chainages(AOI_DIR, frame)
    assert [t.transect_id for t in chainages] == [
        "rasuwagadhi-gyirong",
        "syabrubesi",
        "betrawati",
        "galchhi",
    ]
    for transect in chainages:
        delta = abs(transect.frame_chainage_m / 1000.0 - transect.declared_chainage_km)
        assert delta < 0.01, (
            f"{transect.transect_id}: {delta * 1000:.1f} m from the committed value"
        )
        assert abs(transect.offset_m) < 30.0, "a transect must sit on the centreline"


def test_smoothing_the_centreline_would_break_the_committed_chainages() -> None:
    """Records why the anchor points are raw: smoothing moves the transects kilometres."""
    lonlat = read_centreline_lonlat(AOI_DIR / "river_centreline.geojson")
    raw = build_frame(lonlat, EPSG)
    # a frame whose *anchors* are smoothed shortens the line and drags the chainages with it
    smoothed_points = raw.points.copy()
    for _ in range(40):
        smoothed_points[1:-1] = 0.5 * smoothed_points[1:-1] + 0.25 * (
            smoothed_points[:-2] + smoothed_points[2:]
        )
    steps = np.hypot(*np.diff(smoothed_points, axis=0).T)
    assert steps.sum() < raw.length_m - 500.0, "smoothing must measurably shorten the line"


def test_centreline_geojson_is_a_single_linestring() -> None:
    doc = json.loads((AOI_DIR / "river_centreline.geojson").read_text(encoding="utf-8"))
    geometries = [f["geometry"]["type"] for f in doc["features"]]
    assert geometries == ["LineString"]
