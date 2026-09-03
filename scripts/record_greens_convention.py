"""Record the Syngine responses that pin the force convention and the azimuthal rotation.

Run online once; the resulting fixtures let `tests/unit/adapters/test_greens_convention.py`
prove the convention offline. Every array here is a modelled Syngine response, so the ledger
rows written alongside carry `provenance: derived`, `source: iris_syngine` (ADR-0016).

    uv run python scripts/record_greens_convention.py
"""

# ruff: noqa: T201  (a script; progress goes to stdout)
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from serac.adapters.seismic.syngine import (
    _HORIZONTAL_FORCE,
    _VERTICAL_FORCE,
    ADAPTER_NAME,
    ADAPTER_VERSION,
    LICENCE,
    LICENCE_NOTE,
    LICENCE_SOURCE_URL,
    PROVIDER_URL,
    SyngineGreensLibrary,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.ports.greens import EarthModel, GreensRequest

DISTANCE_DEG = 5.0
DEPTH_M = 1000.0
FORCE_BEARINGS = (0.0, 35.0, 200.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args()
    repo: Path = args.repo.resolve()
    out = repo / "data" / "fixtures" / "greens" / "convention"
    out.mkdir(parents=True, exist_ok=True)

    library = SyngineGreensLibrary(repo / "data" / "interim" / "greens", repo_root=repo)
    request = GreensRequest(
        model=EarthModel.prem_a_20s, distance_deg=DISTANCE_DEG, source_depth_m=DEPTH_M, dt_s=1.0
    )

    vertical = library._call(request, _VERTICAL_FORCE)
    horizontal = library._call(request, _HORIZONTAL_FORCE)
    radial = library._call(request, (0.0, 0.0, 1.0))  # F_east = +1: radial at azimuth 90
    transverse = library._call(request, (0.0, -1.0, 0.0))  # F_north = +1: transverse at az 90

    probe = {}
    for name, block in (
        ("vertical", vertical),
        ("horizontal", horizontal),
        ("radial", radial),
        ("transverse", transverse),
    ):
        for component in ("Z", "N", "E"):
            probe[f"{name}_{component}"] = block[component].astype("float32")
    np.savez_compressed(out / "az90_probe.npz", **probe)

    elementary = SyngineGreensLibrary._elementary_traces(vertical, horizontal)
    named = {
        ("up", "Z"): "Z_v",
        ("up", "R"): "R_v",
        ("north", "Z"): "Z_h",
        ("north", "R"): "R_h",
        ("east", "T"): "T_h",
    }
    rotation = {
        named[(t.force_component, t.receiver_component)]: np.asarray(
            t.samples_m_per_n, dtype="float32"
        )
        for t in elementary
    }
    direct: dict[str, list[np.ndarray]] = {"Z": [], "N": [], "E": []}
    for bearing in FORCE_BEARINGS:
        theta = np.radians(bearing)
        # F_north = cos(bearing), F_east = sin(bearing)  ->  Ft = -cos, Fp = sin
        block = library._call(request, (0.0, -float(np.cos(theta)), float(np.sin(theta))))
        for component in ("Z", "N", "E"):
            direct[component].append(block[component].astype("float32"))
    n = min(len(v) for arrays in direct.values() for v in arrays)
    for component, arrays in direct.items():
        rotation[f"direct_{component}"] = np.vstack([a[:n] for a in arrays])
    for key in ("Z_v", "R_v", "Z_h", "R_h", "T_h"):
        rotation[key] = rotation[key][:n]
    rotation["force_bearings_deg"] = np.asarray(FORCE_BEARINGS, dtype="float64")
    rotation["receiver_azimuth_deg"] = np.asarray(90.0)
    np.savez_compressed(out / "rotation.npz", **rotation)

    # 1-D symmetry, and the geocentric-latitude trap that hides inside it. A receiver placed
    # due north at *geographic* latitude d is not at distance d: Syngine converts to geocentric
    # latitude first. Both the naive and the corrected equatorial twins are recorded so the
    # offline test pins the size of the error as well as the fact that the correction removes it.
    import io

    from obspy import read

    from serac.adapters.seismic.syngine import geocentric_latitude

    def _receiver(lat: float, lon: float) -> dict[str, np.ndarray]:
        params = dict(library._query_params(request, _VERTICAL_FORCE))
        params["receiverlatitude"] = lat
        params["receiverlongitude"] = lon
        response = library.client.get(PROVIDER_URL, params=params, timeout=180.0)
        response.raise_for_status()
        return {
            str(t.stats.channel)[-1]: np.asarray(t.data, dtype=float)
            for t in read(io.BytesIO(response.content), format="MSEED")
        }

    north = _receiver(DISTANCE_DEG, 0.0)
    naive = _receiver(0.0, DISTANCE_DEG)
    corrected = _receiver(0.0, geocentric_latitude(DISTANCE_DEG))
    m = min(len(north["Z"]), len(naive["Z"]), len(corrected["Z"]))

    def _misfit(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.linalg.norm(a[:m] - b[:m]) / np.linalg.norm(b[:m]))

    naive_misfit = _misfit(north["Z"], naive["Z"])
    corrected_misfit = _misfit(north["Z"], corrected["Z"])
    np.savez_compressed(
        out / "symmetry.npz",
        meridional_Z=north["Z"][:m].astype("float32"),
        equatorial_naive_Z=naive["Z"][:m].astype("float32"),
        equatorial_geocentric_Z=corrected["Z"][:m].astype("float32"),
        naive_misfit=np.asarray(naive_misfit),
        geocentric_misfit=np.asarray(corrected_misfit),
        geocentric_lat_deg=np.asarray(geocentric_latitude(DISTANCE_DEG)),
        distance_deg=np.asarray(DISTANCE_DEG),
    )
    print(
        f"1-D symmetry at {DISTANCE_DEG} deg: naive great-circle misfit {naive_misfit:.4f}, "
        f"geocentric-corrected misfit {corrected_misfit:.4f}"
    )

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    retrieved_at = datetime.now(tz=UTC)
    for name in ("az90_probe.npz", "rotation.npz", "symmetry.npz"):
        path = out / name
        ledger.append(
            ManifestEntry(
                source=DataSource.iris_syngine,
                product_id=f"greens/convention/{name}",
                product_level="greens_convention_fixture",
                path=path.resolve().relative_to(repo).as_posix(),
                url=PROVIDER_URL,
                params={
                    "modelled": True,
                    "earth_model": EarthModel.prem_a_20s.value,
                    "distance_deg": DISTANCE_DEG,
                    "source_depth_m": DEPTH_M,
                    "dt_s": 1.0,
                    "purpose": "pins the sourceforce convention and the azimuthal rotation",
                },
                sha256=sha256_of_file(path),
                size_bytes=path.stat().st_size,
                retrieved_at=retrieved_at,
                licence=LICENCE,
                licence_source_url=LICENCE_SOURCE_URL,
                provenance=Provenance.derived,
                status=ManifestStatus.fetched,
                adapter=ADAPTER_NAME,
                adapter_version=ADAPTER_VERSION,
                notes=LICENCE_NOTE,
            )
        )
        print(f"wrote {path} ({path.stat().st_size} B)")
    library.close()


if __name__ == "__main__":
    main()
