"""IRIS Syngine implementation of the `GreensLibrary` port.

Green's functions are **modelled**, not observed: physics evaluated from a published 1-D Earth
model by the Syngine service. Every cached file is ledgered as `provenance: derived`,
`source: iris_syngine`, `params.modelled = true`, under `data/interim/greens/<model>/`
(ADR-0016). They are never published on the bus.

## Why two requests serve every azimuth

In a 1-D Earth the response depends only on (epicentral distance, source depth). A horizontal
force excites the radial pair (Z, R) through its radial projection and the transverse
component T through its transverse projection; a vertical force excites (Z, R) only. So five
elementary responses describe every station at a given distance:

    Z_v, R_v          vertical (up) unit force
    Z_h, R_h          horizontal unit force pointing *along* the source-receiver azimuth
    T_h               horizontal unit force pointing *across* it

`_VERTICAL_FORCE` fetches the first pair. `_HORIZONTAL_FORCE` superposes a radial and a
transverse unit force in one request: because the radial force puts nothing on T and the
transverse force puts nothing on Z or R, the three elementary responses separate cleanly by
component. Two requests per distance, not six, and a 0.5-15 deg library at 0.05 deg is 582
requests rather than 8712.

## Geometry

The source sits at (0, 0) and the receiver at (0, `distance_deg`) — both on the equator, due
east. That is deliberate: Syngine converts geographic to geocentric latitude, so a receiver
placed due *north* of the source is not at exactly `distance_deg`. Along the equator the
conversion is the identity and the epicentral distance is exact. At azimuth 90 the radial
direction is east and the transverse direction is south, which is where the component
extraction in `_elementary_traces` comes from; `tests/unit/adapters/test_greens_convention.py`
pins it against direct per-azimuth Syngine calls rather than against this docstring.

## Syngine's force convention

`sourceforce=Fr,Ft,Fp` in the spherical (r, theta, phi) basis, so `F_up = Fr`,
`F_north = -Ft`, `F_east = Fp`. Verified live, not recalled: an up force returns a Z-dominated
response with an exactly null transverse component.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.errors import SeracError
from serac.ports.greens import (
    EarthModel,
    GreensLibrary,
    GreensPlan,
    GreensRequest,
    GreensSet,
    GreensTrace,
)
from serac.ports.ledger import ManifestLedger

ADAPTER_NAME = "SyngineGreensLibrary"
ADAPTER_VERSION = "0.1.0"

PROVIDER = "IRIS Syngine"
#: `service.iris.edu` 307-redirects here; the resolved host is recorded, never the alias.
PROVIDER_URL = "https://service.iris.edu/irisws/syngine/1/query"
MODELS_URL = "https://service.iris.edu/irisws/syngine/1/models"

LICENCE = "null: see licence_source_url"
LICENCE_SOURCE_URL = "https://ds.iris.edu/ds/products/syngine/"
LICENCE_NOTE = (
    "Syngine synthetic seismograms are computed by EarthScope (formerly IRIS) from published "
    "1-D Earth models with AxiSEM/Instaseis. They are modelled physics, not observations: "
    "serac records them as provenance=derived (ADR-0016). Please cite Krischer et al. (2017) "
    "and the Syngine data product DOI when reusing them."
)

#: (Fr, Ft, Fp) in newtons for the two elementary requests. `Ft` is southward, so
#: `(0, -1, 1)` is one newton north plus one newton east.
_VERTICAL_FORCE = (1.0, 0.0, 0.0)
_HORIZONTAL_FORCE = (0.0, -1.0, 1.0)

#: Order the five elementary traces are stored in; the cache format depends on it.
ELEMENTARY: tuple[tuple[str, str], ...] = (
    ("up", "Z"),
    ("up", "R"),
    ("north", "Z"),
    ("north", "R"),
    ("east", "T"),
)

#: Rough size of one gzipped cache file, used only by `plan()`'s stated estimate basis.
ASSUMED_BYTES_PER_SET = 12_000


class SyngineError(SeracError):
    """A Syngine request or cache read failed."""


class HttpClientLike(Protocol):
    """The subset of `httpx.Client` the adapter uses (tests inject a fake)."""

    def get(self, url: str, *, params: Any = ..., timeout: Any = ...) -> httpx.Response: ...


#: WGS84 flattening, the value Instaseis (and therefore Syngine) uses to convert geographic
#: latitude to geocentric latitude before computing an epicentral distance.
WGS84_FLATTENING = 0.0033528106647474805


def geocentric_latitude(geographic_lat_deg: float) -> float:
    """Geographic to geocentric latitude, `atan((1-f)^2 tan(lat))`.

    This is not a refinement; skipping it is a bug. Syngine computes its epicentral distance
    from geocentric coordinates, so a station looked up at its *geographic* great-circle
    distance is matched to the wrong Green's function. Measured live at 5 deg: the offset is
    0.033 deg (about 3.7 km, a 1 s move of the surface-wave arrival) and the waveform misfit
    between the two conventions is **36%** in the 20-150 s band. Applying the conversion
    drives that misfit to zero to four decimals; `test_greens_convention.py` pins both numbers.
    """
    lat = math.radians(geographic_lat_deg)
    return math.degrees(math.atan((1.0 - WGS84_FLATTENING) ** 2 * math.tan(lat)))


def geocentric_distance_azimuth(
    source_lat: float, source_lon: float, station_lat: float, station_lon: float
) -> tuple[float, float]:
    """`(epicentral distance in degrees, source-to-station azimuth in degrees)`.

    Spherical trigonometry on geocentric latitudes, matching the geometry Syngine used to
    compute the responses. Azimuth is measured clockwise from north at the source and
    returned in [0, 360).
    """
    phi1 = math.radians(geocentric_latitude(source_lat))
    phi2 = math.radians(geocentric_latitude(station_lat))
    dlon = math.radians(station_lon - source_lon)
    cos_delta = math.sin(phi1) * math.sin(phi2) + math.cos(phi1) * math.cos(phi2) * math.cos(dlon)
    delta = math.acos(max(-1.0, min(1.0, cos_delta)))
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    azimuth = math.degrees(math.atan2(y, x)) % 360.0
    return math.degrees(delta), azimuth


def _canonical_distance(distance_deg: float) -> float:
    """Grid distances onto the 0.01 deg lattice the cache key encodes."""
    return round(distance_deg, 2)


def encode_set(greens: GreensSet) -> bytes:
    """Serialise a `GreensSet` to the deterministic on-disk form.

    JSON with base64 float32 payloads, gzipped with `mtime=0` so the same physics always
    hashes to the same bytes. Determinism matters because the fixture checksums in
    `validate-lfh` are the only thing standing between a committed Green's function and a
    silently regenerated one.
    """
    payload = {
        "request": json.loads(greens.request.model_dump_json()),
        "provider": greens.provider,
        "provider_url": greens.provider_url,
        "cache_key": greens.cache_key,
        "modelled": True,
        "traces": [
            {
                "force_component": t.force_component,
                "receiver_component": t.receiver_component,
                "samples_m_per_n_f32_b64": base64.b64encode(
                    np.asarray(t.samples_m_per_n, dtype="<f4").tobytes()
                ).decode("ascii"),
            }
            for t in greens.traces
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as fh:
        fh.write(raw)
    return buffer.getvalue()


def decode_set(blob: bytes, *, retrieved_at: datetime | None, sha256: str) -> GreensSet:
    """Inverse of `encode_set`."""
    payload = json.loads(gzip.decompress(blob).decode("utf-8"))
    traces = [
        GreensTrace(
            force_component=t["force_component"],
            receiver_component=t["receiver_component"],
            samples_m_per_n=np.frombuffer(
                base64.b64decode(t["samples_m_per_n_f32_b64"]), dtype="<f4"
            )
            .astype(float)
            .tolist(),
        )
        for t in payload["traces"]
    ]
    return GreensSet(
        request=GreensRequest.model_validate(payload["request"]),
        traces=traces,
        provider=payload["provider"],
        provider_url=payload["provider_url"],
        retrieved_at_utc=retrieved_at,
        cache_key=payload["cache_key"],
        sha256=sha256,
    )


class SyngineGreensLibrary(GreensLibrary):
    """Cache of Syngine responses on disk, fetched once and reused across grid nodes.

    `plan()` writes nothing. `get()` returns a cached set when one exists and otherwise
    fetches, writes the cache file, updates the index and appends one ledger row.
    """

    def __init__(
        self,
        cache_root: Path,
        *,
        client: HttpClientLike | None = None,
        repo_root: Path | None = None,
        timeout: float = 180.0,
        provider_url: str = PROVIDER_URL,
        allow_network: bool = True,
    ) -> None:
        self.cache_root = cache_root
        self.repo_root = repo_root
        self.timeout = timeout
        self.provider_url = provider_url
        self.allow_network = allow_network
        self._client = client
        self._owns_client = client is None
        self._index_cache: dict[Path, dict[str, dict[str, Any]]] = {}

    # --- plumbing -----------------------------------------------------------------------

    @property
    def client(self) -> HttpClientLike:
        if self._client is None:
            if not self.allow_network:
                raise SyngineError(
                    "Green's function not cached and allow_network=False; build the library "
                    "with `serac lfh greens build` before running offline"
                )
            self._client = httpx.Client(follow_redirects=True, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and isinstance(self._client, httpx.Client):
            self._client.close()
            self._client = None

    def model_dir(self, model: EarthModel) -> Path:
        return self.cache_root / model.value

    def cache_path(self, request: GreensRequest) -> Path:
        return self.model_dir(request.model) / f"{request.cache_key()}.json.gz"

    def index_path(self, model: EarthModel) -> Path:
        return self.model_dir(model) / "index.json"

    def _rel(self, path: Path) -> str:
        if self.repo_root is not None:
            try:
                return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    # --- index --------------------------------------------------------------------------

    def read_index(self, model: EarthModel) -> dict[str, dict[str, Any]]:
        """`cache_key -> {path, sha256, retrieved_at}` for one Earth model."""
        path = self.index_path(model)
        cached = self._index_cache.get(path)
        if cached is not None:
            return cached
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries: dict[str, dict[str, Any]] = dict(raw.get("entries", {}))
        else:
            entries = {}
        self._index_cache[path] = entries
        return entries

    def _write_index(self, model: EarthModel, entries: dict[str, dict[str, Any]]) -> None:
        path = self.index_path(model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model": model.value,
                    "provider": PROVIDER,
                    "provider_url": self.provider_url,
                    "modelled": True,
                    "adapter": ADAPTER_NAME,
                    "adapter_version": ADAPTER_VERSION,
                    "entries": dict(sorted(entries.items())),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._index_cache[path] = entries

    # --- plan ---------------------------------------------------------------------------

    def plan(self, requests: Sequence[GreensRequest]) -> GreensPlan:
        """Say what would be fetched. Writes nothing — not the cache, not the ledger."""
        unique: dict[str, GreensRequest] = {}
        for request in requests:
            unique.setdefault(request.cache_key(), request)
        cached = sum(1 for r in unique.values() if self.cache_path(r).exists())
        to_fetch = len(unique) - cached
        return GreensPlan(
            requests=sorted(unique.values(), key=lambda r: (r.model.value, r.distance_deg)),
            cached=cached,
            to_fetch=to_fetch,
            estimated_bytes=to_fetch * ASSUMED_BYTES_PER_SET,
            estimate_basis=(
                f"{to_fetch} uncached (distance, depth) pairs x {ASSUMED_BYTES_PER_SET} B per "
                "gzipped 5-trace set; two Syngine requests each (one vertical force, one "
                "superposed radial+transverse horizontal force)"
            ),
            provider_url=self.provider_url,
        )

    # --- fetch --------------------------------------------------------------------------

    def _query_params(
        self, request: GreensRequest, force: tuple[float, float, float]
    ) -> dict[str, Any]:
        return {
            "model": request.model.value,
            "sourcelatitude": 0.0,
            "sourcelongitude": 0.0,
            "sourcedepthinmeters": request.source_depth_m,
            "receiverlatitude": 0.0,
            "receiverlongitude": _canonical_distance(request.distance_deg),
            "sourceforce": ",".join(f"{v:g}" for v in force),
            "units": request.units,
            "dt": request.dt_s,
            "components": "ZNE",
            "format": "miniseed",
            "endtime": request.duration_s,
        }

    def _request_url(self, request: GreensRequest, force: tuple[float, float, float]) -> str:
        return str(httpx.URL(self.provider_url, params=self._query_params(request, force)))

    def _call(
        self, request: GreensRequest, force: tuple[float, float, float]
    ) -> dict[str, np.ndarray]:
        params = self._query_params(request, force)
        try:
            response = self.client.get(self.provider_url, params=params, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SyngineError(f"Syngine request failed for {request.cache_key()}: {exc}") from exc
        if response.status_code != 200:
            raise SyngineError(
                f"Syngine returned HTTP {response.status_code} for {request.cache_key()}: "
                f"{response.text[:200]}"
            )
        from obspy import read

        stream = read(io.BytesIO(response.content), format="MSEED")
        out: dict[str, np.ndarray] = {}
        for trace in stream:
            out[str(trace.stats.channel)[-1]] = np.asarray(trace.data, dtype=float)
        missing = {"Z", "N", "E"} - set(out)
        if missing:
            raise SyngineError(f"Syngine response missing components {sorted(missing)}")
        return out

    @staticmethod
    def _elementary_traces(
        vertical: dict[str, np.ndarray], horizontal: dict[str, np.ndarray]
    ) -> list[GreensTrace]:
        """Split the two responses into the five elementary traces.

        With the source at (0, 0) and the receiver at (0, d), the azimuth is 90 deg, so the
        radial direction is east and the transverse unit vector `T = (-sin az, cos az)` in
        (north, east) is due south. Hence `T = -N`.

        The vertical request carries `F_up = 1`, so `Z_v = Z` and `R_v = E`.

        The horizontal request carries `F_north = 1` (transverse projection `F_t = -1`) and
        `F_east = 1` (radial projection `F_r = +1`). The radial force alone reaches Z and R,
        so `Z_h = Z` and `R_h = E`; the transverse force alone reaches T, so
        `T = F_t * T_h = -T_h` and, since `T = -N`, `T_h = N`.

        The 3-azimuth test in `test_greens_convention.py` is what actually proves this.
        """
        length = min(len(vertical["Z"]), len(horizontal["Z"]))
        arrays = {
            ("up", "Z"): vertical["Z"][:length],
            ("up", "R"): vertical["E"][:length],
            ("north", "Z"): horizontal["Z"][:length],
            ("north", "R"): horizontal["E"][:length],
            ("east", "T"): horizontal["N"][:length],
        }
        return [
            GreensTrace(
                force_component=force,  # type: ignore[arg-type]
                receiver_component=receiver,  # type: ignore[arg-type]
                samples_m_per_n=arrays[(force, receiver)].astype(float).tolist(),
            )
            for force, receiver in ELEMENTARY
        ]

    def fetch(self, request: GreensRequest) -> GreensSet:
        """Two Syngine calls, split into the five elementary traces. No cache, no ledger."""
        vertical = self._call(request, _VERTICAL_FORCE)
        horizontal = self._call(request, _HORIZONTAL_FORCE)
        traces = self._elementary_traces(vertical, horizontal)
        provisional = GreensSet(
            request=request,
            traces=traces,
            provider=PROVIDER,
            provider_url=self.provider_url,
            retrieved_at_utc=datetime.now(tz=UTC),
            cache_key=request.cache_key(),
            sha256="0" * 64,
        )
        blob = encode_set(provisional)
        return provisional.model_copy(update={"sha256": hashlib.sha256(blob).hexdigest()})

    # --- port ---------------------------------------------------------------------------

    def get(self, request: GreensRequest, ledger: ManifestLedger) -> GreensSet:
        path = self.cache_path(request)
        index = self.read_index(request.model)
        if path.exists():
            blob = path.read_bytes()
            digest = hashlib.sha256(blob).hexdigest()
            recorded = index.get(request.cache_key(), {})
            expected = recorded.get("sha256")
            if expected is not None and expected != digest:
                raise SyngineError(
                    f"cached Green's function {self._rel(path)} does not match its index "
                    f"sha256 ({digest} != {expected}); refusing to invert on it"
                )
            retrieved = recorded.get("retrieved_at")
            return decode_set(
                blob,
                retrieved_at=datetime.fromisoformat(retrieved) if retrieved else None,
                sha256=digest,
            )

        greens = self.fetch(request)
        blob = encode_set(greens)
        digest = hashlib.sha256(blob).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        retrieved_at = greens.retrieved_at_utc or datetime.now(tz=UTC)
        index[request.cache_key()] = {
            "path": self._rel(path),
            "sha256": digest,
            "retrieved_at": retrieved_at.isoformat(),
        }
        self._write_index(request.model, index)
        ledger.append(self._entry(request, path, digest, retrieved_at, len(blob)))
        return greens.model_copy(update={"sha256": digest})

    def _entry(
        self,
        request: GreensRequest,
        path: Path,
        digest: str,
        retrieved_at: datetime,
        size_bytes: int,
    ) -> ManifestEntry:
        return ManifestEntry(
            source=DataSource.iris_syngine,
            product_id=f"greens/{request.model.value}/{request.cache_key()}",
            product_level="greens_function",
            path=self._rel(path),
            url=self._request_url(request, _VERTICAL_FORCE),
            params={
                "modelled": True,
                "earth_model": request.model.value,
                "distance_deg": _canonical_distance(request.distance_deg),
                "source_depth_m": request.source_depth_m,
                "dt_s": request.dt_s,
                "duration_s": request.duration_s,
                "units": request.units,
                "force_basis": request.force_basis,
                "requests": [
                    self._request_url(request, _VERTICAL_FORCE),
                    self._request_url(request, _HORIZONTAL_FORCE),
                ],
                "elementary_traces": [f"{f}->{r}" for f, r in ELEMENTARY],
                "geometry": "source (0,0), receiver (0,distance_deg); equatorial, azimuth 90",
            },
            sha256=digest,
            size_bytes=size_bytes,
            retrieved_at=retrieved_at,
            licence=LICENCE,
            licence_source_url=LICENCE_SOURCE_URL,
            provenance=Provenance.derived,
            status=ManifestStatus.fetched,
            adapter=ADAPTER_NAME,
            adapter_version=ADAPTER_VERSION,
            notes=LICENCE_NOTE,
        )


def distance_library(
    *,
    min_deg: float = 0.5,
    max_deg: float = 15.0,
    step_deg: float = 0.05,
    model: EarthModel = EarthModel.prem_a_20s,
    source_depth_m: float = 1000.0,
    dt_s: float = 1.0,
    duration_s: float = 900.0,
) -> list[GreensRequest]:
    """The distance library: one request per lattice distance, all azimuths served by rotation."""
    n = round((max_deg - min_deg) / step_deg) + 1
    return [
        GreensRequest(
            model=model,
            distance_deg=_canonical_distance(min_deg + i * step_deg),
            source_depth_m=source_depth_m,
            dt_s=dt_s,
            duration_s=duration_s,
        )
        for i in range(n)
    ]


def nearest_request(
    distance_deg: float,
    *,
    model: EarthModel = EarthModel.prem_a_20s,
    source_depth_m: float = 1000.0,
    dt_s: float = 1.0,
    duration_s: float = 900.0,
    step_deg: float = 0.05,
    min_deg: float = 0.5,
    max_deg: float = 15.0,
) -> GreensRequest:
    """Snap a station distance onto the library lattice."""
    clamped = min(max(distance_deg, min_deg), max_deg)
    snapped = min_deg + round((clamped - min_deg) / step_deg) * step_deg
    return GreensRequest(
        model=model,
        distance_deg=_canonical_distance(snapped),
        source_depth_m=source_depth_m,
        dt_s=dt_s,
        duration_s=duration_s,
    )


def rotate_to_zne(
    greens: dict[tuple[str, str], np.ndarray], azimuth_deg: float
) -> dict[str, np.ndarray]:
    """Design-matrix columns for one station: component -> (3, n) rows per unit force.

    Returns `{"Z": M, "N": M, "E": M}` where each `M` has rows ordered
    `(F_up, F_north, F_east)`: the displacement that station would record per newton of each
    force component, as a function of time.

    The radial unit vector at azimuth `phi` is `(cos phi, sin phi)` in (north, east) and the
    transverse unit vector is `(-sin phi, cos phi)`, so a horizontal force `(F_n, F_e)`
    projects as `F_r = F_n cos phi + F_e sin phi` and `F_t = -F_n sin phi + F_e cos phi`.
    Substituting into `Z = F_up Z_v + F_r Z_h`, `R = F_up R_v + F_r R_h`, `T = F_t T_h` and
    rotating (R, T) back to (N, E) gives the matrices below.
    """
    phi = math.radians(azimuth_deg)
    c, s = math.cos(phi), math.sin(phi)
    z_v = greens[("up", "Z")]
    r_v = greens[("up", "R")]
    z_h = greens[("north", "Z")]
    r_h = greens[("north", "R")]
    t_h = greens[("east", "T")]
    return {
        "Z": np.vstack([z_v, c * z_h, s * z_h]),
        "N": np.vstack([c * r_v, c * c * r_h + s * s * t_h, c * s * (r_h - t_h)]),
        "E": np.vstack([s * r_v, c * s * (r_h - t_h), s * s * r_h + c * c * t_h]),
    }


def as_arrays(greens: GreensSet) -> dict[tuple[str, str], np.ndarray]:
    """`(force_component, receiver_component) -> samples` for `rotate_to_zne`."""
    return {
        (t.force_component, t.receiver_component): np.asarray(t.samples_m_per_n, dtype=float)
        for t in greens.traces
    }
