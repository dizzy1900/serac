"""ASF HyP3 **burst** InSAR (`INSAR_ISCE_MULTI_BURST`), with stream-crop-delete retention.

Why a second HyP3 adapter rather than a parameter on `Hyp3InsarAdapter`: burst InSAR is a
different job type with a different granule vocabulary (burst granules, not SLC scenes), a
different product layout, a different cost tier and — decisively for this machine — products
one order of magnitude smaller. The live HyP3 `/costs` table on 2026-09-03 prices
`INSAR_GAMMA` at 10 credits per pair against 1 credit for a multi-burst pair of up to four
bursts, and a full-frame GAMMA product is 300 MB to 1 GB against roughly 50 MB for three
bursts. Both matter here: the disk is the binding constraint on the whole prompt.

Retention
---------
A delivered zip is hashed on arrival, cropped to the AOI, and then **deleted**. Its ledger row
carries `retention: transient` and says so in `notes`. That is a genuinely weaker provenance
guarantee than a retained file — `validate-ingest` cannot re-hash bytes that no longer exist —
and it is recorded as such rather than papered over. The crops that survive are ordinary
`retention: retained` rows and are re-hashable.

Credentials
-----------
The account here has an Earthdata **bearer token**, not a username and password, so this
adapter declares its own `CredentialSpec` over `EARTHDATA_TOKEN` and talks to the HyP3 REST
API directly over HTTP. The token is read through `SeracSettings` as a `SecretStr` and is
never written to the ledger, a report, or a log line.
"""

from __future__ import annotations

import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol

import httpx

from serac.adapters.eo._base import RAW_DIRNAME, BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import sha256_and_size
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Retention
from serac.errors import SeracError
from serac.ports.ingest import (
    ConfirmFn,
    CredentialSpec,
    DryRunPlan,
    IngestRequest,
    ProductRecord,
)
from serac.ports.ledger import ManifestLedger

HYP3_API_URL: Final[str] = "https://hyp3-api.asf.alaska.edu"
JOB_TYPE: Final[str] = "INSAR_ISCE_MULTI_BURST"
PRODUCT_LEVEL: Final[str] = "INSAR_ISCE_MULTI_BURST"
DEFAULT_LOOKS: Final[str] = "20x4"
CHUNK_BYTES: Final[int] = 1 << 20

HYP3_LICENCE: Final[str] = (
    "HyP3 products are derived from Copernicus Sentinel data (Sentinel Data Legal Notice) and "
    "distributed by ASF free of charge; attribution 'ASF DAAC HyP3 [year]; contains modified "
    "Copernicus Sentinel data [year], processed by ESA'"
)
HYP3_LICENCE_URL: Final[str] = "https://hyp3-docs.asf.alaska.edu/using/credits/"

EARTHDATA_TOKEN_CREDENTIAL: Final[CredentialSpec] = CredentialSpec(
    name="Earthdata Login bearer token",
    env_vars=("EARTHDATA_TOKEN",),
    purpose="submit and download ASF HyP3 jobs (the burst search itself is public)",
)

RETAINED_SUFFIXES: Final[tuple[str, ...]] = (
    "_unw_phase.tif",
    "_corr.tif",
    "_dem.tif",
    "_lv_theta.tif",
    "_lv_phi.tif",
    "_water_mask.tif",
)
"""The rasters MintPy's HyP3 loader needs. Everything else in the zip is discarded."""

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED"})


class Hyp3Error(SeracError):
    """The HyP3 API refused a request or returned something unusable."""


@dataclass(frozen=True)
class BurstJob:
    """One HyP3 job as the API reports it."""

    job_id: str
    status: str
    name: str = ""
    files: tuple[dict[str, Any], ...] = ()
    expiration: str | None = None
    processing_times: tuple[float, ...] = ()

    @property
    def zip_url(self) -> str | None:
        for f in self.files:
            filename = str(f.get("filename", ""))
            if filename.endswith(".zip"):
                url = f.get("url")
                return str(url) if url else None
        return None

    @property
    def zip_size(self) -> int | None:
        for f in self.files:
            if str(f.get("filename", "")).endswith(".zip"):
                size = f.get("size")
                return int(size) if size is not None else None
        return None


class Hyp3BurstClient(Protocol):
    """The four calls this adapter makes. Fakes script whole job lifecycles offline."""

    def submit(
        self, reference: Sequence[str], secondary: Sequence[str], *, name: str, looks: str
    ) -> BurstJob: ...

    def jobs_by_name(self, name: str) -> list[BurstJob]: ...

    def get_job(self, job_id: str) -> BurstJob: ...

    def download(self, url: str, dest: Path) -> tuple[str, int]: ...


def _job_from_payload(payload: dict[str, Any]) -> BurstJob:
    return BurstJob(
        job_id=str(payload.get("job_id", "")),
        status=str(payload.get("status_code", "")),
        name=str(payload.get("name", "")),
        files=tuple(payload.get("files") or ()),
        expiration=payload.get("expiration_time"),
        processing_times=tuple(float(t) for t in (payload.get("processing_times") or ())),
    )


class Hyp3HttpClient:
    """`Hyp3BurstClient` over the HyP3 REST API with an Earthdata bearer token.

    The token is held as a plain string only inside this object and only ever leaves it in an
    `Authorization` header. It is not logged, not stored, and not put in any exception message.
    """

    def __init__(self, token: str, *, api_url: str = HYP3_API_URL, timeout_s: float = 60.0) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout_s

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, follow_redirects=True, headers=self._headers)

    def user(self) -> dict[str, Any]:
        """`/user`: application status and remaining credits."""
        with self._client() as client:
            response = client.get(f"{self._api_url}/user")
            _raise_for_status(response)
            payload: dict[str, Any] = response.json()
        return payload

    def submit(
        self, reference: Sequence[str], secondary: Sequence[str], *, name: str, looks: str
    ) -> BurstJob:
        body = {
            "jobs": [
                {
                    "job_type": JOB_TYPE,
                    "name": name[:100],
                    "job_parameters": {
                        "reference": list(reference),
                        "secondary": list(secondary),
                        "looks": looks,
                        "apply_water_mask": False,
                    },
                }
            ]
        }
        with self._client() as client:
            response = client.post(f"{self._api_url}/jobs", json=body)
            _raise_for_status(response)
            jobs = response.json().get("jobs") or []
        if not jobs:
            raise Hyp3Error("HyP3 accepted the request but returned no job")
        return _job_from_payload(jobs[0])

    def jobs_by_name(self, name: str) -> list[BurstJob]:
        out: list[BurstJob] = []
        params: dict[str, Any] = {"name": name[:100]}
        with self._client() as client:
            while True:
                response = client.get(f"{self._api_url}/jobs", params=params)
                _raise_for_status(response)
                payload = response.json()
                out.extend(_job_from_payload(j) for j in payload.get("jobs") or [])
                token = payload.get("next")
                if not token:
                    return out
                params = {"start_token": token, "name": name[:100]}

    def get_job(self, job_id: str) -> BurstJob:
        with self._client() as client:
            response = client.get(f"{self._api_url}/jobs/{job_id}")
            _raise_for_status(response)
            return _job_from_payload(response.json())

    def download(self, url: str, dest: Path) -> tuple[str, int]:
        """Stream one product zip to `dest`, returning (sha256, size)."""
        import hashlib

        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(60.0, read=600.0),
                    follow_redirects=True,
                    headers=self._headers,
                ) as client,
                client.stream("GET", url) as response,
            ):
                _raise_for_status(response)
                with part.open("wb") as fh:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        digest.update(chunk)
                        size += len(chunk)
                        fh.write(chunk)
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), size


def _raise_for_status(response: httpx.Response) -> None:
    """Raise `Hyp3Error` with the API's own message, never echoing the request headers."""
    if response.status_code < 400:
        return
    detail = ""
    try:
        detail = str(response.json())[:400]
    except ValueError:  # pragma: no cover - non-JSON error body
        detail = response.text[:400] if not response.is_stream_consumed else ""
    raise Hyp3Error(
        f"HyP3 {response.request.method} {response.request.url.path}: "
        f"HTTP {response.status_code} {detail}"
    )


# -- extraction ---------------------------------------------------------------------------------


@dataclass
class ExtractedProduct:
    """What survived a delivered zip: the rasters MintPy needs plus the metadata sidecar."""

    product_id: str
    rasters: list[Path] = field(default_factory=list)
    metadata: Path | None = None


def extract_wanted(
    zip_path: Path, dest: Path, *, suffixes: Sequence[str] = RETAINED_SUFFIXES
) -> ExtractedProduct:
    """Extract only the wanted members of a HyP3 burst product zip, flattened into `dest`.

    HyP3 zips contain one directory named after the product. Members are matched by suffix so
    a future addition to the product bundle is ignored rather than silently retained.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out = ExtractedProduct(product_id=zip_path.stem)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            name = Path(member).name
            if not name:
                continue
            if any(name.endswith(s) for s in suffixes):
                target = dest / name
                with zf.open(member) as src, target.open("wb") as fh:
                    while chunk := src.read(CHUNK_BYTES):
                        fh.write(chunk)
                out.rasters.append(target)
            elif name.endswith(".txt") and not name.endswith("README.md.txt"):
                target = dest / name
                with zf.open(member) as src, target.open("wb") as fh:
                    while chunk := src.read(CHUNK_BYTES):
                        fh.write(chunk)
                out.metadata = target
    out.rasters.sort()
    return out


# -- adapter ------------------------------------------------------------------------------------


class Hyp3BurstInsarAdapter(BaseIngestAdapter):
    """Submit, poll, download, crop and delete `INSAR_ISCE_MULTI_BURST` jobs.

    `search`/`plan` exist to satisfy the `IngestAdapter` port; the watch pipeline drives the
    lifecycle through `submit_pair`, `job_state` and `harvest` because a five-year SBAS network
    is a long-running, resumable job rather than a single fetch.
    """

    source: ClassVar[DataSource] = DataSource.hyp3_insar
    adapter_name: ClassVar[str] = "hyp3_burst_insar"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = HYP3_LICENCE
    licence_source_url: ClassVar[str | None] = HYP3_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (EARTHDATA_TOKEN_CREDENTIAL,)

    def __init__(self, client: Hyp3BurstClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client

    @property
    def client(self) -> Hyp3BurstClient:
        if self._client is None:
            token = self.settings.earthdata_token
            if token is None:
                raise Hyp3Error("EARTHDATA_TOKEN is not set; see docs/CREDENTIALS.md")
            self._client = Hyp3HttpClient(token.get_secret_value())
        return self._client

    # -- port surface -------------------------------------------------------------------------

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        """Burst-pair discovery lives in `models.watch.network`; nothing to search here."""
        return []

    def plan(self, request: IngestRequest) -> DryRunPlan:
        return self.build_plan(
            request,
            [],
            estimated_bytes=None,
            estimate_basis=(
                "HyP3 publishes no product size before a job completes; the watch pipeline's "
                "`serac watch plan-network --dry-run` costs the network instead"
            ),
            warnings=["use `serac watch submit-insar`; this adapter is driven pair by pair"],
        )

    def fetch(
        self,
        plan: DryRunPlan,
        *,
        dest_root: Path,
        ledger: ManifestLedger,
        confirm: ConfirmFn,
    ) -> list[ManifestEntry]:
        raise NotImplementedError(
            "burst InSAR is driven by `serac watch submit-insar` / `poll-insar`, not `fetch`"
        )

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:  # pragma: no cover - `fetch` refuses first
        raise NotImplementedError

    # -- lifecycle ----------------------------------------------------------------------------

    def work_dir(self, dest_root: Path, aoi_id: str) -> Path:
        return dest_root / RAW_DIRNAME / "hyp3_burst_insar" / aoi_id

    def submit_pair(
        self,
        *,
        pair_id: str,
        reference: Sequence[str],
        secondary: Sequence[str],
        looks: str,
        job_name: str,
    ) -> BurstJob:
        return self.client.submit(reference, secondary, name=job_name, looks=looks)

    def record_submission(
        self,
        *,
        job: BurstJob,
        pair: ProductRecord,
        request: IngestRequest,
        ledger: ManifestLedger,
    ) -> ManifestEntry:
        return ledger.append(
            self._record(
                pair,
                request,
                status=ManifestStatus.requested,
                params={"job_id": job.job_id, "job_status": job.status, "job_type": JOB_TYPE},
                product_level=PRODUCT_LEVEL,
                notes=(
                    "HyP3 INSAR_ISCE_MULTI_BURST job submitted; poll with `serac watch poll-insar`"
                ),
            )
        )

    def harvest(
        self,
        *,
        job: BurstJob,
        pair: ProductRecord,
        request: IngestRequest,
        ledger: ManifestLedger,
        dest_root: Path,
        crop: CropFn | None = None,
    ) -> list[ManifestEntry]:
        """Download, hash, extract, crop, then delete the zip. One ledger row per file.

        The zip's row is `status: fetched` with `retention: transient`: the sha256 was computed
        on the bytes as they arrived, which is honest, but the bytes are gone and the row can
        never be re-verified. `validate-ingest` surfaces exactly these rows as a warning.
        """
        url = job.zip_url
        if url is None:
            raise Hyp3Error(f"job {job.job_id} succeeded but exposes no product zip")
        work = self.work_dir(dest_root, request.aoi_id)
        staging = work / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / f"{pair.product_id}.zip"
        entries: list[ManifestEntry] = []
        try:
            sha, size = self.client.download(url, zip_path)
            entries.append(
                ledger.append(
                    self._record(
                        pair,
                        request,
                        status=ManifestStatus.fetched,
                        path=zip_path,
                        sha256=sha,
                        size_bytes=size,
                        url=url,
                        params={"job_id": job.job_id, "job_type": JOB_TYPE},
                        product_level=PRODUCT_LEVEL,
                        notes=(
                            "HyP3 burst InSAR product zip; hashed on arrival, cropped to the AOI, "
                            "then deleted to fit the disk budget (retention: transient, so this "
                            "row cannot be re-hashed)"
                        ),
                        retention=Retention.transient,
                    )
                )
            )
            extracted = extract_wanted(zip_path, staging / pair.product_id)
            kept = crop(extracted, work / pair.product_id) if crop is not None else extracted
            for path in sorted([*kept.rasters, *([kept.metadata] if kept.metadata else [])]):
                file_sha, file_size = sha256_and_size(path)
                entries.append(
                    ledger.append(
                        self._record(
                            pair,
                            request,
                            status=ManifestStatus.fetched,
                            path=path,
                            sha256=file_sha,
                            size_bytes=file_size,
                            url=url,
                            params={"job_id": job.job_id, "derived_from_zip_sha256": sha},
                            product_level=PRODUCT_LEVEL,
                            notes=(
                                "AOI crop of a HyP3 burst InSAR raster; the source zip was deleted"
                            ),
                        )
                    )
                )
        finally:
            zip_path.unlink(missing_ok=True)
            _remove_tree(staging / pair.product_id)
        return entries

    def wait_for(
        self, job_ids: Sequence[str], *, poll_s: float = 60.0, timeout_s: float = 6 * 3600
    ) -> dict[str, BurstJob]:  # pragma: no cover - exercised online only
        """Poll until every job reaches a terminal status or `timeout_s` elapses."""
        deadline = time.monotonic() + timeout_s
        state: dict[str, BurstJob] = {}
        pending = list(job_ids)
        while pending and time.monotonic() < deadline:
            for job_id in list(pending):
                job = self.client.get_job(job_id)
                state[job_id] = job
                if job.status in TERMINAL_STATUSES:
                    pending.remove(job_id)
            if pending:
                time.sleep(poll_s)
        return state


CropFn = Any
"""`Callable[[ExtractedProduct, Path], ExtractedProduct]`; typed loosely to keep this module
free of a rasterio import (the cropper lives in `models.watch.crop`)."""


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
