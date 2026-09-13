"""GEOGLOWS ECMWF streamflow as an open hydrometric feed (`HydrometricSource`).

Nepal DHM gauges have no stable open API (RELEASE_STATUS.md Known gaps 2). The GEOGLOWS v2
REST service at `https://geoglows.ecmwf.int/api/` is the honest global substitute: modelled
discharge on TDX-Hydro reaches, free, no key. Retrospective daily values are ERA5-driven
simulations, not gauge observations. The adapter fetches or fails; it never synthesises
discharges.

Offline tests drive a committed cut of one real retrospective response (Trishuli near Galchhi,
reach `441020026`). A fixture whose `status` is `not_fetched` raises
`DatasetNotFetchedError` on every call, the same contract as the ICIMOD adapter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from serac.errors import DatasetNotFetchedError, SeracError
from serac.ports.seismic import HydrometricSource, HydroObservation, HydroStation

ADAPTER_NAME = "geoglows_ecmwf"
ADAPTER_VERSION = "0.1.0"
BASE_URL = "https://geoglows.ecmwf.int/api/v2"
LICENCE = (
    "CC-BY-4.0 as stated at https://geoglows.ecmwf.int/license (GEOGLOWS ECMWF Streamflow "
    "Service Data). Retrospective is ERA5-driven; ERA5 is a Copernicus C3S product under the "
    "Licence to Use Copernicus Products "
    "(https://apps.ecmwf.int/datasets/licences/copernicus/)."
)
LICENCE_SOURCE_URL = "https://geoglows.ecmwf.int/license"
SOURCE_REF = "geoglows-ecmwf-v2"
DEFAULT_FIXTURE = (
    Path("data") / "fixtures" / "hydro" / "geoglows" / "reach_441020026_retrospectivedaily.json"
)
# Query point used to identify the committed Galchhi/Trishuli reach; not the reach centroid.
DEFAULT_QUERY_LAT = 27.81
DEFAULT_QUERY_LON = 85.00
DEFAULT_REACH_ID = "441020026"


class GeoglowsError(SeracError):
    """The GEOGLOWS payload is missing or malformed."""


class GeoglowsHttpClient(Protocol):
    """The one GET the live adapter makes; tests inject a fake that serves fixtures."""

    def get_json(self, url: str) -> Any: ...


class HttpxGeoglowsClient:
    """Production client over `httpx`."""

    def __init__(self, timeout_s: float = 60.0) -> None:
        self._timeout_s = timeout_s
        self._client: Any = None

    def get_json(self, url: str) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        response = self._client.get(url)
        response.raise_for_status()
        return response.json()


def retrospectivedaily_url(reach_id: int | str) -> str:
    return f"{BASE_URL}/retrospectivedaily/{reach_id}?format=json"


def getriverid_url(lat: float, lon: float) -> str:
    return f"{BASE_URL}/getriverid?lat={lat}&lon={lon}"


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _as_mapping(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GeoglowsError("GEOGLOWS payload must be a JSON object")
    return payload


def parse_reach_id(payload: Mapping[str, Any]) -> str:
    """Reach id from metadata, a numeric key, or `river_id`."""
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("river_id") is not None:
        return str(metadata["river_id"])
    if payload.get("river_id") is not None:
        return str(payload["river_id"])
    numeric = [k for k in payload if k.isdigit()]
    if len(numeric) == 1:
        return numeric[0]
    raise GeoglowsError("GEOGLOWS payload does not name a river_id")


def parse_flow_series(payload: Mapping[str, Any]) -> tuple[list[datetime], list[float]]:
    """Parallel datetime / discharge arrays. Never invents a missing series."""
    times_raw = payload.get("datetime")
    if not isinstance(times_raw, list) or not times_raw:
        raise GeoglowsError("GEOGLOWS payload has no datetime series")
    if "flow_median" in payload:
        values_raw: object = payload["flow_median"]
    else:
        reach_id = parse_reach_id(payload)
        values_raw = payload.get(reach_id)
        if values_raw is None:
            raise GeoglowsError(f"GEOGLOWS payload has no discharge series for reach {reach_id}")
    if not isinstance(values_raw, list) or len(values_raw) != len(times_raw):
        raise GeoglowsError(
            "GEOGLOWS datetime and discharge series are missing or different lengths"
        )
    times = [_parse_time(str(t)) for t in times_raw]
    values: list[float] = []
    for item in values_raw:
        if not isinstance(item, (int, float)):
            raise GeoglowsError("GEOGLOWS discharge series contains a non-numeric value")
        values.append(float(item))
    return times, values


def load_fixture_document(path: Path) -> dict[str, Any]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetNotFetchedError(f"GEOGLOWS fixture missing: {path}") from exc
    return _as_mapping(raw)


class GeoglowsHydrometric(HydrometricSource):
    """Modelled GEOGLOWS discharge for one TDX-Hydro reach; fetch or fail, never synthesise."""

    def __init__(
        self,
        fixture_path: Path | None = None,
        *,
        payload: Mapping[str, Any] | None = None,
        client: GeoglowsHttpClient | None = None,
        query_lat: float | None = None,
        query_lon: float | None = None,
    ) -> None:
        self.fixture_path = fixture_path if fixture_path is not None else DEFAULT_FIXTURE
        self._given_payload = dict(payload) if payload is not None else None
        self._client = client
        self.query_lat = DEFAULT_QUERY_LAT if query_lat is None else query_lat
        self.query_lon = DEFAULT_QUERY_LON if query_lon is None else query_lon
        self._document: dict[str, Any] | None = None

    @property
    def client(self) -> GeoglowsHttpClient:
        if self._client is None:
            self._client = HttpxGeoglowsClient()
        return self._client

    def _document_load(self) -> dict[str, Any]:
        if self._document is not None:
            return self._document
        if self._given_payload is not None:
            self._document = dict(self._given_payload)
            return self._document
        self._document = load_fixture_document(self.fixture_path)
        return self._document

    def _require_fetched(self) -> dict[str, Any]:
        doc = self._document_load()
        status = doc.get("status")
        if status == "not_fetched":
            reason = str(doc.get("reason") or "no data")
            raise DatasetNotFetchedError(f"{self.fixture_path}: status=not_fetched; {reason}")
        if "payload" in doc and isinstance(doc["payload"], dict):
            return doc["payload"]
        return doc

    def fetch_retrospective_daily(self, reach_id: int | str) -> dict[str, Any]:
        """GET one reach's retrospective daily series, or raise. Never synthesises."""
        url = retrospectivedaily_url(reach_id)
        try:
            body = self.client.get_json(url)
        except Exception as exc:
            raise DatasetNotFetchedError(f"GEOGLOWS fetch failed for {url}: {exc}") from exc
        if not isinstance(body, dict) or not body:
            raise DatasetNotFetchedError(f"GEOGLOWS returned no JSON object from {url}")
        self._given_payload = body
        self._document = body
        return body

    def nearest_reach_id(self, lat: float, lon: float) -> str:
        url = getriverid_url(lat, lon)
        try:
            body = self.client.get_json(url)
        except Exception as exc:
            raise DatasetNotFetchedError(f"GEOGLOWS getriverid failed for {url}: {exc}") from exc
        if not isinstance(body, dict) or body.get("river_id") is None:
            raise DatasetNotFetchedError(f"GEOGLOWS getriverid returned no river_id from {url}")
        return str(body["river_id"])

    def stations(self) -> list[HydroStation]:
        payload = self._require_fetched()
        reach_id = parse_reach_id(payload)
        return [
            HydroStation(
                station_id=reach_id,
                name=f"GEOGLOWS TDX-Hydro reach {reach_id} (Trishuli near Galchhi query point)",
                river="Trishuli",
                latitude=self.query_lat,
                longitude=self.query_lon,
                operator="GEOGLOWS ECMWF Streamflow Service",
                source_refs=[SOURCE_REF],
            )
        ]

    def observations(
        self, station_id: str, window: tuple[datetime, datetime]
    ) -> list[HydroObservation]:
        payload = self._require_fetched()
        reach_id = parse_reach_id(payload)
        if station_id != reach_id:
            raise DatasetNotFetchedError(f"no GEOGLOWS reach {station_id!r} in {self.fixture_path}")
        start, end = window
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
        times, values = parse_flow_series(payload)
        raw_meta = payload.get("metadata")
        metadata: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        series_raw = metadata.get("series")
        series = (
            series_raw if isinstance(series_raw, str) and series_raw else "retrospectivedaily"
        )
        raw_units = metadata.get("units")
        units: dict[str, Any] = raw_units if isinstance(raw_units, dict) else {}
        unit_short = str(units.get("short") or "cms")
        if unit_short not in {"cms", "m3/s", "m^3/s"}:
            raise GeoglowsError(f"GEOGLOWS units {unit_short!r} are not cubic metres per second")
        time_basis = (
            "geoglows_retrospective_daily"
            if "flow_median" not in payload
            else "geoglows_forecast_median"
        )
        out: list[HydroObservation] = []
        for when, value in zip(times, values, strict=True):
            if start <= when <= end:
                out.append(
                    HydroObservation(
                        station_id=reach_id,
                        time_utc=when,
                        time_basis=time_basis,
                        variable="discharge_m3s",
                        value=value,
                        source_ref=SOURCE_REF,
                        notes=(
                            f"GEOGLOWS modelled {series} streamflow, not a DHM gauge reading; "
                            f"units {unit_short}"
                        ),
                    )
                )
        if not out:
            raise DatasetNotFetchedError(
                f"no GEOGLOWS discharges for {station_id!r} in the window; the fixture holds "
                "only the retrieved series, never a synthesised gap-fill"
            )
        return out
