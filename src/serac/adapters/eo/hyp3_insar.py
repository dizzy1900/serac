"""ASF HyP3 on-demand Sentinel-1 InSAR: pair planning, job submission, watch, download.

`InSARPairPlanner` turns a Sentinel-1 SLC listing into same-track pairs (same `pathNumber`,
`flightDirection` and frame, 0 < dt <= 12 days by default). `Hyp3InsarAdapter` submits one
INSAR_GAMMA job per pair through a `Hyp3Client` Protocol (production: `hyp3-sdk`), keeps a
jobs ledger at `data/raw/hyp3_insar/<aoi>/jobs.jsonl`, records every submission as
`status: requested` in the provenance ledger, and flips a pair to `fetched` (one entry per
file, hashed) once the job has succeeded and its products are downloaded.

HyP3 does not publish product sizes before a job completes, so plans carry
`estimated_bytes = None` and the base adapter's confirmation gate asks before fetching.
Without Earthdata Login, `fetch` records `not_fetched` and raises.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.adapters.eo._asf import EARTHDATA_CREDENTIAL, AsfSearchClient
from serac.adapters.eo._base import RAW_DIRNAME, BaseIngestAdapter, FetchedFile
from serac.adapters.eo._http import sha256_and_size
from serac.adapters.eo.asf_sentinel1 import Sentinel1AsfAdapter
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus
from serac.errors import IngestRefusedError
from serac.ports.ingest import (
    ConfirmFn,
    CredentialSpec,
    DryRunPlan,
    IngestRequest,
    ProductRecord,
)
from serac.ports.ledger import ManifestLedger

HYP3_API_URL = "https://hyp3-api.asf.alaska.edu"
HYP3_LICENCE = (
    "HyP3 products are derived from Copernicus Sentinel data (Sentinel Data Legal Notice) and "
    "distributed by ASF free of charge; attribution 'ASF DAAC HyP3 [year] using GAMMA software; "
    "contains modified Copernicus Sentinel data [year], processed by ESA'"
)
HYP3_LICENCE_URL = "https://hyp3-docs.asf.alaska.edu/using/credits/"
PRODUCT_LEVEL = "INSAR_GAMMA"
DEFAULT_MAX_DAYS = 12
DEFAULT_LOOKS: Literal["20x4", "10x2"] = "20x4"
JOBS_FILENAME = "jobs.jsonl"
JobStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]
DONE_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED"})


# -- pair planning ------------------------------------------------------------------------------


@dataclass(frozen=True)
class InSARPair:
    reference: ProductRecord
    secondary: ProductRecord

    @property
    def path_number(self) -> int:
        return int(self.reference.properties["pathNumber"])

    @property
    def flight_direction(self) -> str:
        return str(self.reference.properties.get("flightDirection"))

    @property
    def dt_days(self) -> float:
        assert self.reference.time_start and self.secondary.time_start
        delta = self.secondary.time_start - self.reference.time_start
        return delta.total_seconds() / 86_400.0

    @property
    def pair_id(self) -> str:
        assert self.reference.time_start and self.secondary.time_start
        return (
            f"S1_{self.path_number:03d}_{self.reference.time_start:%Y%m%d}_"
            f"{self.secondary.time_start:%Y%m%d}"
        )


class InSARPairPlanner:
    """Same-track SLC pairs with 0 < dt <= `max_days`, grouped by path, direction and frame."""

    def __init__(self, *, max_days: float = DEFAULT_MAX_DAYS, frame_tolerance: int = 0) -> None:
        if max_days <= 0:
            raise ValueError("max_days must be > 0")
        self.max_days = max_days
        self.frame_tolerance = frame_tolerance

    def plan_pairs(self, products: Sequence[ProductRecord]) -> list[InSARPair]:
        groups: dict[tuple[int, str], list[ProductRecord]] = defaultdict(list)
        for p in products:
            if p.product_level != "SLC" or p.time_start is None:
                continue
            path = p.properties.get("pathNumber")
            if path is None:
                continue
            groups[(int(path), str(p.properties.get("flightDirection")))].append(p)
        pairs: list[InSARPair] = []
        for _key, scenes in sorted(groups.items()):
            scenes.sort(key=lambda p: (p.time_start, p.product_id))
            for i, ref in enumerate(scenes):
                for sec in scenes[i + 1 :]:
                    pair = InSARPair(ref, sec)
                    if pair.dt_days <= 0:
                        continue
                    if pair.dt_days > self.max_days:
                        break
                    if not self._frames_match(ref, sec):
                        continue
                    pairs.append(pair)
        return sorted(pairs, key=lambda p: (p.reference.time_start, p.pair_id))

    def _frames_match(self, a: ProductRecord, b: ProductRecord) -> bool:
        fa, fb = a.properties.get("frameNumber"), b.properties.get("frameNumber")
        if fa is None or fb is None:
            return False
        return abs(int(fa) - int(fb)) <= self.frame_tolerance


# -- HyP3 client --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hyp3JobInfo:
    job_id: str
    status: str
    name: str | None = None
    files: tuple[dict[str, Any], ...] = ()
    expiration: str | None = None


class Hyp3Client(Protocol):
    """The four HyP3 calls the adapter makes; fakes script job lifecycles."""

    def submit_insar_job(
        self, reference: str, secondary: str, *, name: str, looks: str
    ) -> Hyp3JobInfo: ...

    def get_job(self, job_id: str) -> Hyp3JobInfo: ...

    def watch(self, job_id: str, *, timeout_s: float) -> Hyp3JobInfo: ...

    def download(self, job_id: str, dest: Path) -> list[Path]: ...


class Hyp3SdkClient:
    """`Hyp3Client` over `hyp3_sdk.HyP3`; the production choice."""

    def __init__(self, username: str, password: str, api_url: str = HYP3_API_URL) -> None:
        self._username = username
        self._password = password
        self._api_url = api_url
        self._hyp3: Any = None

    def _open(self) -> Any:
        if self._hyp3 is None:
            from hyp3_sdk import HyP3

            self._hyp3 = HyP3(
                api_url=self._api_url, username=self._username, password=self._password
            )
        return self._hyp3

    @staticmethod
    def _info(job: Any) -> Hyp3JobInfo:
        return Hyp3JobInfo(
            job_id=str(job.job_id),
            status=str(job.status_code),
            name=job.name,
            files=tuple(job.files or ()),
            expiration=job.expiration_time.isoformat() if job.expiration_time else None,
        )

    def submit_insar_job(
        self,
        reference: str,
        secondary: str,
        *,
        name: str,
        looks: str,
        include_dem: bool = True,
    ) -> Hyp3JobInfo:
        """Submit one interferogram.

        `include_dem` defaults to True because MintPy's HyP3 loader needs the DEM and the
        look-vector rasters to build a time series; without them a stack cannot be inverted.
        It costs one extra raster per pair.
        """
        batch = self._open().submit_insar_job(
            reference,
            secondary,
            name=name,
            include_los_displacement=True,
            include_look_vectors=True,
            include_dem=include_dem,
            looks=looks,
        )
        return self._info(batch.jobs[0])

    def get_job(self, job_id: str) -> Hyp3JobInfo:
        return self._info(self._open().get_job_by_id(job_id))

    def watch(self, job_id: str, *, timeout_s: float) -> Hyp3JobInfo:
        job = self._open().get_job_by_id(job_id)
        return self._info(self._open().watch(job, timeout=int(timeout_s)))

    def download(self, job_id: str, dest: Path) -> list[Path]:
        job = self._open().get_job_by_id(job_id)
        paths: list[Path] = [Path(p) for p in job.download_files(dest)]
        return paths


# -- jobs ledger --------------------------------------------------------------------------------


class Hyp3JobRecord(BaseModel):
    """One line of `data/raw/hyp3_insar/<aoi>/jobs.jsonl`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    job_id: str
    status: str
    name: str
    reference: str
    secondary: str
    aoi_id: str
    looks: str = DEFAULT_LOOKS
    submitted_at: AwareDatetime
    updated_at: AwareDatetime
    files: list[str] = Field(default_factory=list)
    manifest_entry_ids: list[str] = Field(default_factory=list)


class JobsLedger:
    """Append-only job bookkeeping; the latest row per pair wins."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: Hyp3JobRecord) -> Hyp3JobRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
        return record

    def rows(self) -> Iterator[Hyp3JobRecord]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield Hyp3JobRecord.model_validate_json(line)

    def latest_by_pair(self) -> dict[str, Hyp3JobRecord]:
        out: dict[str, Hyp3JobRecord] = {}
        for r in self.rows():
            prev = out.get(r.pair_id)
            if prev is None or r.updated_at >= prev.updated_at:
                out[r.pair_id] = r
        return out


def jobs_ledger_path(dest_root: Path, aoi_id: str) -> Path:
    return dest_root / RAW_DIRNAME / DataSource.hyp3_insar.value / aoi_id / JOBS_FILENAME


# -- adapter --------------------------------------------------------------------------------------


class Hyp3InsarAdapter(BaseIngestAdapter):
    """Plan pairs from an ASF SLC listing; submit, watch and download HyP3 INSAR_GAMMA jobs."""

    source: ClassVar[DataSource] = DataSource.hyp3_insar
    adapter_name: ClassVar[str] = "hyp3_insar"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = HYP3_LICENCE
    licence_source_url: ClassVar[str | None] = HYP3_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (EARTHDATA_CREDENTIAL,)

    def __init__(
        self,
        search_client: AsfSearchClient | None = None,
        *,
        hyp3: Hyp3Client | None = None,
        planner: InSARPairPlanner | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.s1 = Sentinel1AsfAdapter(search_client, **kwargs)
        self._hyp3 = hyp3
        self.planner = planner or InSARPairPlanner()

    @property
    def hyp3(self) -> Hyp3Client:
        if self._hyp3 is None:
            user = self.settings.earthdata_username
            password = self.settings.earthdata_password
            if user is None or password is None:
                raise RuntimeError("Earthdata credentials missing; fetch() should have refused")
            self._hyp3 = Hyp3SdkClient(user.get_secret_value(), password.get_secret_value())
        return self._hyp3

    # -- search / plan --------------------------------------------------------------------------

    def _pair_record(self, pair: InSARPair) -> ProductRecord:
        ref, sec = pair.reference, pair.secondary
        return ProductRecord(
            source=self.source,
            product_id=pair.pair_id,
            product_level=PRODUCT_LEVEL,
            url=None,
            time_start=ref.time_start,
            time_end=sec.time_start,
            bbox_4326=ref.bbox_4326,
            estimated_bytes=None,
            licence=self.licence,
            licence_source_url=self.licence_source_url,
            properties={
                "reference": ref.product_id,
                "secondary": sec.product_id,
                "pathNumber": pair.path_number,
                "flightDirection": pair.flight_direction,
                "frameNumber": ref.properties.get("frameNumber"),
                "dt_days": round(pair.dt_days, 3),
                "looks": DEFAULT_LOOKS,
            },
        )

    def pairs(self, request: IngestRequest) -> list[InSARPair]:
        slc_request = request.model_copy(
            update={"product_level": "SLC", "params": {**request.params, "processing_level": "SLC"}}
        )
        max_days = float(request.params.get("max_days", self.planner.max_days))
        planner = InSARPairPlanner(max_days=max_days, frame_tolerance=self.planner.frame_tolerance)
        return planner.plan_pairs(self.s1.search(slc_request))

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        return [self._pair_record(p) for p in self.pairs(request)]

    def plan(self, request: IngestRequest) -> DryRunPlan:
        products = self.search(request)
        warnings: list[str] = []
        if not products:
            warnings.append("no same-track SLC pairs within the day limit")
        per_path: dict[int, int] = defaultdict(int)
        for p in products:
            per_path[int(p.properties["pathNumber"])] += 1
        if per_path:
            warnings.append(
                "pairs per relative orbit: "
                + ", ".join(f"path {k}: {v}" for k, v in sorted(per_path.items()))
            )
        warnings.append(
            "each pair is one HyP3 INSAR_GAMMA job; jobs draw on the account's monthly credits"
        )
        return self.build_plan(
            request,
            products,
            estimated_bytes=None,
            estimate_basis=(
                "HyP3 does not publish product sizes before a job completes; unknown until "
                "the job succeeds"
            ),
            warnings=warnings,
        )

    # -- fetch: submit / watch / download ---------------------------------------------------------

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:  # pragma: no cover - `fetch` is overridden; kept for the ABC
        raise NotImplementedError("Hyp3InsarAdapter.fetch drives the job lifecycle itself")

    def fetch(
        self,
        plan: DryRunPlan,
        *,
        dest_root: Path,
        ledger: ManifestLedger,
        confirm: ConfirmFn,
    ) -> list[ManifestEntry]:
        if plan.refusals:
            raise IngestRefusedError("; ".join(plan.refusals))
        self.refuse_without_credentials(plan, ledger)
        self.confirm_gate(plan, ledger, confirm)
        request = plan.request
        jobs = JobsLedger(jobs_ledger_path(dest_root, request.aoi_id))
        wait = bool(request.params.get("wait", False))
        timeout_s = float(request.params.get("timeout_s", 3 * 3600))
        entries: list[ManifestEntry] = []
        for product in plan.products:
            entries.extend(
                self._advance(product, request, dest_root, ledger, jobs, wait, timeout_s)
            )
        return entries

    def poll(
        self, plan: DryRunPlan, *, dest_root: Path, ledger: ManifestLedger
    ) -> list[ManifestEntry]:
        """Refresh job states and download what has succeeded; never submits."""
        request = plan.request
        jobs = JobsLedger(jobs_ledger_path(dest_root, request.aoi_id))
        known = jobs.latest_by_pair()
        entries: list[ManifestEntry] = []
        for product in plan.products:
            if product.product_id in known:
                entries.extend(self._advance(product, request, dest_root, ledger, jobs, False, 0.0))
        return entries

    def _advance(
        self,
        product: ProductRecord,
        request: IngestRequest,
        dest_root: Path,
        ledger: ManifestLedger,
        jobs: JobsLedger,
        wait: bool,
        timeout_s: float,
    ) -> list[ManifestEntry]:
        now = self._clock()
        record = jobs.latest_by_pair().get(product.product_id)
        if record is None:
            info = self.hyp3.submit_insar_job(
                str(product.properties["reference"]),
                str(product.properties["secondary"]),
                name=f"serac-{request.aoi_id}-{product.product_id}",
                looks=str(product.properties.get("looks", DEFAULT_LOOKS)),
            )
            entry = ledger.append(
                self._record(
                    product,
                    request,
                    status=ManifestStatus.requested,
                    params={
                        "job_id": info.job_id,
                        "job_status": info.status,
                        "jobs_ledger": self._relative_path(jobs.path),
                        **product.properties,
                    },
                    notes="HyP3 INSAR_GAMMA job submitted; poll with `serac ingest hyp3 --poll`",
                )
            )
            record = jobs.append(
                Hyp3JobRecord(
                    pair_id=product.product_id,
                    job_id=info.job_id,
                    status=info.status,
                    name=info.name or "",
                    reference=str(product.properties["reference"]),
                    secondary=str(product.properties["secondary"]),
                    aoi_id=request.aoi_id,
                    looks=str(product.properties.get("looks", DEFAULT_LOOKS)),
                    submitted_at=now,
                    updated_at=now,
                    manifest_entry_ids=[entry.entry_id],
                )
            )
            if not wait:
                return [entry]
        if record.status == "FAILED" or (record.status == "SUCCEEDED" and record.files):
            return []  # already recorded as failed, or downloaded and recorded
        info = (
            self.hyp3.watch(record.job_id, timeout_s=timeout_s)
            if wait and record.status not in DONE_STATUSES
            else self.hyp3.get_job(record.job_id)
        )
        if info.status == "FAILED":
            entry = ledger.append(
                self._record(
                    product,
                    request,
                    status=ManifestStatus.failed,
                    params={"job_id": record.job_id, "job_status": info.status},
                    notes="HyP3 job failed",
                )
            )
            jobs.append(
                record.model_copy(
                    update={
                        "status": info.status,
                        "updated_at": self._clock(),
                        "manifest_entry_ids": [*record.manifest_entry_ids, entry.entry_id],
                    }
                )
            )
            return [entry]
        if info.status != "SUCCEEDED":
            if info.status != record.status:
                jobs.append(record.model_copy(update={"status": info.status, "updated_at": now}))
            return []
        dest = self.product_dir(dest_root, request.aoi_id, product.product_id)
        dest.mkdir(parents=True, exist_ok=True)
        paths = self.hyp3.download(record.job_id, dest)
        files: list[FetchedFile] = []
        for path in paths:
            sha, size = sha256_and_size(path)
            files.append(
                FetchedFile(
                    path=path,
                    sha256=sha,
                    size_bytes=size,
                    url=_file_url(info, path.name),
                    params={
                        "job_id": record.job_id,
                        "job_status": info.status,
                        **product.properties,
                    },
                    notes="HyP3 INSAR_GAMMA product file (GAMMA, geocoded, 80 m at 20x4 looks)",
                    product_level=PRODUCT_LEVEL,
                )
            )
        entries = self.record_files(product, request, files, ledger)
        jobs.append(
            record.model_copy(
                update={
                    "status": info.status,
                    "updated_at": self._clock(),
                    "files": [self._relative_path(p) for p in paths],
                    "manifest_entry_ids": [
                        *record.manifest_entry_ids,
                        *(e.entry_id for e in entries),
                    ],
                }
            )
        )
        return entries


def _file_url(info: Hyp3JobInfo, filename: str) -> str | None:
    for f in info.files:
        if str(f.get("filename", "")) == filename:
            url = f.get("url")
            return str(url) if url else None
    return None


def load_jobs(path: Path) -> list[dict[str, Any]]:
    """Plain dict rows of a jobs ledger (for `serac ingest hyp3 --poll` output)."""
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
