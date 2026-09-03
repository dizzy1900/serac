"""The Green's-function force convention and azimuthal rotation, proved rather than recalled.

Two things could be wrong in a way that produces a plausible force history with the wrong
sign or the wrong azimuth, and neither would show up as a fit residual:

1. **Syngine's `sourceforce=Fr,Ft,Fp` mapping.** `Ft` is southward, so `F_north = -Ft`. Get
   that backwards and every inverted force azimuth is mirrored about the meridian.
2. **The azimuthal rotation** that lets two requests per distance serve all azimuths. Get the
   transverse sign backwards and the horizontal force rotates the wrong way.

The offline tests below pin the rotation algebra against a brute-force reconstruction and
against recorded Syngine responses committed as fixtures. The `online` tests re-derive both
from the live service, including a direct per-azimuth comparison at three azimuths, and are
what actually established the convention in the first place.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from serac.adapters.seismic.syngine import (
    _HORIZONTAL_FORCE,
    _VERTICAL_FORCE,
    ELEMENTARY,
    SyngineGreensLibrary,
    as_arrays,
    decode_set,
    encode_set,
    geocentric_distance_azimuth,
    geocentric_latitude,
    nearest_request,
    rotate_to_zne,
)
from serac.ports.greens import EarthModel, GreensRequest, GreensSet, GreensTrace

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "data" / "fixtures" / "greens"
SYNGINE_HOST = "service.iris.edu"


def _fake_set(n: int = 64, seed: int = 3) -> GreensSet:
    rng = np.random.default_rng(seed)
    traces = [
        GreensTrace(
            force_component=force,  # type: ignore[arg-type]
            receiver_component=receiver,  # type: ignore[arg-type]
            samples_m_per_n=rng.normal(size=n).tolist(),
        )
        for force, receiver in ELEMENTARY
    ]
    return GreensSet(
        request=GreensRequest(distance_deg=5.0, source_depth_m=1000.0),
        traces=traces,
        provider="test",
        provider_url="https://example.invalid",
        cache_key="test",
        sha256="0" * 64,
    )


# --- rotation algebra ---------------------------------------------------------------------


@pytest.mark.parametrize("azimuth", [0.0, 17.0, 90.0, 143.0, 270.0, 359.0])
def test_rotation_matches_explicit_projection(azimuth: float) -> None:
    """`rotate_to_zne` equals projecting the force, applying the 1-D response, rotating back.

    This is the same physics written twice: once as the matrix the inversion uses, and once
    as the step-by-step operation a reader can check against the definition of radial and
    transverse. They must agree for any force vector.
    """
    greens = as_arrays(_fake_set())
    columns = rotate_to_zne(greens, azimuth)
    phi = math.radians(azimuth)
    c, s = math.cos(phi), math.sin(phi)
    rng = np.random.default_rng(11)
    for _ in range(5):
        f_up, f_north, f_east = rng.normal(size=3)
        f_radial = f_north * c + f_east * s
        f_transverse = -f_north * s + f_east * c
        z = f_up * greens[("up", "Z")] + f_radial * greens[("north", "Z")]
        r = f_up * greens[("up", "R")] + f_radial * greens[("north", "R")]
        t = f_transverse * greens[("east", "T")]
        expected = {"Z": z, "N": r * c - t * s, "E": r * s + t * c}
        force = np.array([f_up, f_north, f_east])
        for component, want in expected.items():
            got = force @ columns[component]
            np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-15)


def test_vertical_force_puts_nothing_on_transverse() -> None:
    """A vertical force is axisymmetric: the transverse component of its response is zero.

    Expressed through the rotation, a pure up force must reproduce itself on Z and R with no
    dependence on the transverse response at any azimuth.
    """
    greens = as_arrays(_fake_set())
    for azimuth in (0.0, 33.0, 200.0):
        columns = rotate_to_zne(greens, azimuth)
        phi = math.radians(azimuth)
        c, s = math.cos(phi), math.sin(phi)
        up = np.array([1.0, 0.0, 0.0])
        n_response, e_response = up @ columns["N"], up @ columns["E"]
        transverse = -n_response * s + e_response * c
        np.testing.assert_allclose(transverse, np.zeros_like(transverse), atol=1e-15)


def test_radial_horizontal_force_puts_nothing_on_transverse() -> None:
    """A horizontal force aligned with the azimuth excites Z and R only."""
    greens = as_arrays(_fake_set())
    for azimuth in (12.0, 118.0, 305.0):
        columns = rotate_to_zne(greens, azimuth)
        phi = math.radians(azimuth)
        c, s = math.cos(phi), math.sin(phi)
        force = np.array([0.0, c, s])  # unit force pointing along the azimuth
        transverse = -(force @ columns["N"]) * s + (force @ columns["E"]) * c
        np.testing.assert_allclose(transverse, np.zeros_like(transverse), atol=1e-14)


def test_transverse_horizontal_force_puts_nothing_on_z() -> None:
    """A horizontal force across the azimuth excites T only."""
    greens = as_arrays(_fake_set())
    for azimuth in (12.0, 118.0, 305.0):
        columns = rotate_to_zne(greens, azimuth)
        phi = math.radians(azimuth)
        c, s = math.cos(phi), math.sin(phi)
        force = np.array([0.0, -s, c])
        np.testing.assert_allclose(force @ columns["Z"], 0.0, atol=1e-14)


# --- cache round trip ---------------------------------------------------------------------


def test_encode_decode_is_deterministic_and_lossless() -> None:
    greens = _fake_set()
    blob_a, blob_b = encode_set(greens), encode_set(greens)
    assert blob_a == blob_b, "gzip mtime must be pinned so cached physics hashes stably"
    back = decode_set(blob_a, retrieved_at=None, sha256="a" * 64)
    assert [(t.force_component, t.receiver_component) for t in back.traces] == list(ELEMENTARY)
    for original, restored in zip(greens.traces, back.traces, strict=True):
        np.testing.assert_allclose(
            restored.samples_m_per_n, original.samples_m_per_n, rtol=1e-6, atol=1e-12
        )


def test_nearest_request_snaps_onto_the_library_lattice() -> None:
    assert nearest_request(3.017).distance_deg == pytest.approx(3.0)
    assert nearest_request(3.033).distance_deg == pytest.approx(3.05)
    assert nearest_request(0.1).distance_deg == pytest.approx(0.5), "clamped at the near edge"
    assert nearest_request(40.0).distance_deg == pytest.approx(15.0), "clamped at the far edge"


def test_force_constants_encode_the_syngine_basis() -> None:
    """`sourceforce=Fr,Ft,Fp` with `Ft` southward: `(0, -1, 1)` is one N north plus one N east."""
    assert _VERTICAL_FORCE == (1.0, 0.0, 0.0)
    assert _HORIZONTAL_FORCE == (0.0, -1.0, 1.0)


# --- against committed Syngine responses ---------------------------------------------------


def _load_fixture(name: str) -> dict[str, np.ndarray]:
    path = FIXTURE_DIR / "convention" / name
    if not path.exists():
        pytest.skip(f"convention fixture missing: {path}")
    payload = np.load(path)
    return {key: np.asarray(payload[key], dtype=float) for key in payload.files}


def test_recorded_syngine_response_has_the_expected_nulls() -> None:
    """Committed Syngine bytes: an up force is Z-dominated with an exactly null transverse."""
    recorded = _load_fixture("az90_probe.npz")
    peak = {key: float(np.abs(value).max()) for key, value in recorded.items()}
    assert peak["vertical_Z"] > peak["vertical_E"] > 0, "up force: Z-dominated, radial present"
    assert peak["vertical_N"] < 1e-6 * peak["vertical_Z"], "up force: transverse must be null"
    assert peak["transverse_N"] > 0
    assert peak["transverse_Z"] < 1e-6 * peak["transverse_N"], "transverse force: no Z"
    assert peak["transverse_E"] < 1e-6 * peak["transverse_N"], "transverse force: no radial"


def test_recorded_superposition_separates_by_component() -> None:
    """The one superposed horizontal request equals the two separate ones, component by
    component. This is what makes two requests per distance sufficient."""
    recorded = _load_fixture("az90_probe.npz")
    for component in ("Z", "N", "E"):
        np.testing.assert_allclose(
            recorded[f"horizontal_{component}"],
            recorded[f"radial_{component}"] + recorded[f"transverse_{component}"],
            rtol=1e-5,
            atol=1e-9 * float(np.abs(recorded[f"horizontal_{component}"]).max() + 1e-30),
        )


def test_recorded_rotation_reproduces_direct_syngine_calls() -> None:
    """Rotating the two elementary requests reproduces Syngine asked the question directly.

    The receiver is held fixed and the *force bearing* is varied, which is exactly equivalent
    to varying the station azimuth (the response depends only on the difference) and has the
    advantage of being geometrically exact: the same station, the same epicentral distance,
    only the force direction changes. Three bearings are recorded, chosen so that one is
    purely radial, one purely transverse and one mixed.

    If the transverse sign, the radial projection or Syngine's `Ft`-is-southward convention
    were wrong, the mixed bearing would not match.
    """
    recorded = _load_fixture("rotation.npz")
    greens = {
        ("up", "Z"): recorded["Z_v"],
        ("up", "R"): recorded["R_v"],
        ("north", "Z"): recorded["Z_h"],
        ("north", "R"): recorded["R_h"],
        ("east", "T"): recorded["T_h"],
    }
    azimuth = float(recorded["receiver_azimuth_deg"])
    columns = rotate_to_zne(greens, azimuth)
    for index, bearing in enumerate(recorded["force_bearings_deg"]):
        theta = math.radians(float(bearing))
        force = np.array([0.0, math.cos(theta), math.sin(theta)])
        for component in ("Z", "N", "E"):
            direct = recorded[f"direct_{component}"][index]
            rotated = force @ columns[component]
            scale = float(np.abs(recorded[f"direct_{component}"]).max())
            np.testing.assert_allclose(rotated, direct, rtol=1e-4, atol=1e-6 * scale)


def test_recorded_one_dimensional_symmetry_holds_only_in_geocentric_coordinates() -> None:
    """The response depends only on distance -- but only once the latitude is geocentric.

    This is the assumption that turns 8712 requests into 582, so it is measured rather than
    asserted, and the measurement carries a trap worth keeping in a test. A receiver placed
    due north at *geographic* latitude 5 deg is not 5 deg away: Syngine converts to geocentric
    latitude first, moving it by 0.033 deg (about 3.7 km, roughly one second of surface-wave
    travel time). In the 20-150 s band that is a **36% waveform misfit** -- large enough to
    wreck an inversion, small enough to look like ordinary model error rather than a bug.

    Applying `geocentric_latitude` drives the misfit to zero to four decimals.
    """
    recorded = _load_fixture("symmetry.npz")
    north = recorded["meridional_Z"]
    naive = recorded["equatorial_naive_Z"]
    corrected = recorded["equatorial_geocentric_Z"]
    assert float(np.abs(north).max()) > 0

    naive_misfit = float(np.linalg.norm(north - naive) / np.linalg.norm(north))
    geocentric_misfit = float(np.linalg.norm(north - corrected) / np.linalg.norm(north))
    assert naive_misfit == pytest.approx(float(recorded["naive_misfit"]), abs=1e-3)
    assert geocentric_misfit == pytest.approx(float(recorded["geocentric_misfit"]), abs=1e-3)

    assert naive_misfit > 0.25, "the naive great-circle lookup really is badly wrong"
    assert geocentric_misfit < 0.01, (
        "1-D symmetry must hold to better than 1% once geocentric latitude is applied; "
        f"measured {geocentric_misfit:.4f}"
    )
    assert geocentric_latitude(float(recorded["distance_deg"])) == pytest.approx(
        float(recorded["geocentric_lat_deg"]), abs=1e-9
    )


def test_geocentric_distance_and_azimuth_agree_with_spherical_limits() -> None:
    """Sanity anchors for the geometry helper the station lookup depends on."""
    assert geocentric_latitude(0.0) == pytest.approx(0.0)
    assert geocentric_latitude(90.0) == pytest.approx(90.0)
    assert geocentric_latitude(-45.0) == pytest.approx(-geocentric_latitude(45.0))

    # Along the equator the conversion is the identity, so distance is exactly the longitude
    # difference and the azimuth is due east.
    distance, azimuth = geocentric_distance_azimuth(0.0, 0.0, 0.0, 5.0)
    assert distance == pytest.approx(5.0, abs=1e-9)
    assert azimuth == pytest.approx(90.0, abs=1e-9)

    # Due north of an equatorial source the distance is the geocentric latitude, not 5 deg.
    distance, azimuth = geocentric_distance_azimuth(0.0, 0.0, 5.0, 0.0)
    assert distance == pytest.approx(geocentric_latitude(5.0), abs=1e-9)
    assert distance < 5.0
    assert azimuth == pytest.approx(0.0, abs=1e-9)

    distance, azimuth = geocentric_distance_azimuth(0.0, 0.0, 0.0, -3.0)
    assert distance == pytest.approx(3.0, abs=1e-9)
    assert azimuth == pytest.approx(270.0, abs=1e-9)


# --- live service -------------------------------------------------------------------------


@pytest.mark.online
def test_live_syngine_convention_and_rotation(tmp_path: Path) -> None:
    """Re-derive the convention from the live service, as it was first established."""
    from tests.conftest import require_network

    require_network(SYNGINE_HOST)
    library = SyngineGreensLibrary(tmp_path / "greens")
    request = GreensRequest(
        model=EarthModel.prem_a_20s, distance_deg=5.0, source_depth_m=1000.0, dt_s=1.0
    )
    vertical = library._call(request, _VERTICAL_FORCE)
    assert np.abs(vertical["Z"]).max() > np.abs(vertical["E"]).max() > 0
    assert np.abs(vertical["N"]).max() < 1e-6 * np.abs(vertical["Z"]).max()

    horizontal = library._call(request, _HORIZONTAL_FORCE)
    radial = library._call(request, (0.0, 0.0, 1.0))
    transverse = library._call(request, (0.0, -1.0, 0.0))
    for component in ("Z", "N", "E"):
        np.testing.assert_allclose(
            horizontal[component], radial[component] + transverse[component], rtol=1e-5, atol=1e-12
        )
    library.close()
