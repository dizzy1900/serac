"""`BaseIngestAdapter`: the shared plan/fetch/ledger mechanics every EO adapter inherits.

What lives here and nowhere else:

* the on-disk layout `data/raw/<source>/<aoi>/<product>/`;
* the confirmation gate (`SIZE_GATE_BYTES`, 5 GiB): a fetch whose estimate exceeds it, or
  cannot be estimated, asks `confirm` first and records `not_fetched` when declined;
* the credentials-missing path: `not_fetched` entries for every product, then
  `CredentialsMissingError` so the CLI exits non-zero;
* `_record`, the single place that builds `ManifestEntry` objects with `adapter`,
  `adapter_version` and `serac_git_sha` filled in.

Subclasses implement `search`, `plan` (usually through `build_plan`) and `_fetch_product`.
"""

from __future__ import annotations

import subprocess
from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from serac.domain.manifest import (
    DataSource,
    ManifestEntry,
    ManifestStatus,
    Provenance,
    Retention,
)
from serac.errors import CredentialsMissingError, FetchDeclinedError, IngestRefusedError
from serac.ports.ingest import (
    ConfirmFn,
    CredentialSpec,
    DryRunPlan,
    IngestAdapter,
    IngestRequest,
    ProductRecord,
)
from serac.ports.ledger import ManifestLedger
from serac.settings import SeracSettings, get_settings

SIZE_GATE_BYTES = 5 * 1024**3
"""Brief non-negotiable 7: ask before any download > 5 GB."""

RAW_DIRNAME = "raw"


@lru_cache(maxsize=4)
def serac_git_sha(repo_root: str | None = None) -> str | None:
    """`git rev-parse HEAD` for provenance, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


@dataclass(frozen=True)
class FetchedFile:
    """What `_fetch_product` hands back for each file it wrote."""

    path: Path
    sha256: str
    size_bytes: int
    url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
    product_level: str | None = None


def product_dir(dest_root: Path, source: DataSource, aoi_id: str, product_id: str) -> Path:
    """`<dest_root>/raw/<source>/<aoi>/<product>/`; product ids never contain path separators."""
    safe_product = product_id.replace("/", "_").replace("\\", "_")
    return dest_root / RAW_DIRNAME / source.value / aoi_id / safe_product


class BaseIngestAdapter(IngestAdapter):
    """Common mechanics; see the module docstring."""

    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str]
    licence_source_url: ClassVar[str | None] = None
    credentials: ClassVar[tuple[CredentialSpec, ...]] = ()
    size_gate_bytes: ClassVar[int] = SIZE_GATE_BYTES

    def __init__(
        self,
        *,
        settings: SeracSettings | None = None,
        repo_root: Path | None = None,
        git_sha: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self._git_sha = git_sha if git_sha is not None else serac_git_sha(str(self.repo_root))
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    # -- settings / credentials -------------------------------------------------------------

    @property
    def settings(self) -> SeracSettings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def missing_credentials(self) -> list[CredentialSpec]:
        """Credentials declared by the adapter whose settings fields are unset."""
        missing: list[CredentialSpec] = []
        for spec in self.credentials:
            values = [getattr(self.settings, var.lower(), None) for var in spec.env_vars]
            if any(v is None for v in values):
                missing.append(spec)
        return missing

    # -- plan helpers -------------------------------------------------------------------------

    def build_plan(
        self,
        request: IngestRequest,
        products: Sequence[ProductRecord],
        *,
        estimated_bytes: int | None,
        estimate_basis: str,
        warnings: Sequence[str] = (),
        refusals: Sequence[str] = (),
    ) -> DryRunPlan:
        """Assemble a `DryRunPlan`, adding the gate warning and the credential list."""
        warn = list(warnings)
        if estimated_bytes is None:
            warn.append("size cannot be estimated; fetch will ask for confirmation")
        elif estimated_bytes > self.size_gate_bytes:
            warn.append(
                f"estimated {estimated_bytes:,} B exceeds the {self.size_gate_bytes:,} B gate; "
                "fetch will ask for confirmation"
            )
        return DryRunPlan(
            source=self.source,
            adapter=self.adapter_name,
            adapter_version=self.adapter_version,
            request=request,
            products=list(products),
            estimated_bytes=estimated_bytes,
            estimate_basis=estimate_basis,
            requires_credentials=self.missing_credentials(),
            warnings=warn,
            refusals=list(refusals),
        )

    # -- fetch ------------------------------------------------------------------------------

    @abstractmethod
    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:
        """Write the product's files under `dest` and describe each one."""

    def product_dir(self, dest_root: Path, aoi_id: str, product_id: str) -> Path:
        """Where a product's files go; fixture builders override this."""
        return product_dir(dest_root, self.source, aoi_id, product_id)

    def refuse_without_credentials(self, plan: DryRunPlan, ledger: ManifestLedger) -> None:
        """Record `not_fetched` for every product and raise when a credential is missing."""
        missing = self.missing_credentials()
        if not missing:
            return
        names = ", ".join(f"{m.name} ({', '.join(m.env_vars)})" for m in missing)
        for product in plan.products:
            ledger.append(
                self._record(
                    product,
                    plan.request,
                    status=ManifestStatus.not_fetched,
                    notes=f"credentials missing: {names}; see {missing[0].docs}",
                )
            )
        raise CredentialsMissingError(
            f"{self.adapter_name} needs {names}; recorded not_fetched for "
            f"{len(plan.products)} product(s). See {missing[0].docs}."
        )

    def confirm_gate(self, plan: DryRunPlan, ledger: ManifestLedger, confirm: ConfirmFn) -> None:
        """Ask before an unestimated or over-gate fetch; record `not_fetched` and raise on no."""
        if plan.estimated_bytes is not None and plan.estimated_bytes <= self.size_gate_bytes:
            return
        size = "unknown size" if plan.estimated_bytes is None else f"{plan.estimated_bytes:,} B"
        question = (
            f"{self.adapter_name}: fetch {len(plan.products)} product(s), {size} "
            f"(gate {self.size_gate_bytes:,} B). Proceed?"
        )
        if confirm(question):
            return
        for product in plan.products:
            ledger.append(
                self._record(
                    product,
                    plan.request,
                    status=ManifestStatus.not_fetched,
                    notes=f"declined at the confirmation gate ({size})",
                )
            )
        raise FetchDeclinedError(question)

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
        request = plan.request
        self.refuse_without_credentials(plan, ledger)
        self.confirm_gate(plan, ledger, confirm)
        entries: list[ManifestEntry] = []
        for product in plan.products:
            dest = self.product_dir(dest_root, request.aoi_id, product.product_id)
            dest.mkdir(parents=True, exist_ok=True)
            try:
                files = self._fetch_product(product, dest, request)
            except Exception as exc:
                ledger.append(
                    self._record(
                        product,
                        request,
                        status=ManifestStatus.failed,
                        notes=f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
                raise
            entries.extend(self.record_files(product, request, files, ledger))
        return entries

    def record_files(
        self,
        product: ProductRecord,
        request: IngestRequest,
        files: Sequence[FetchedFile],
        ledger: ManifestLedger,
    ) -> list[ManifestEntry]:
        """Append one `fetched` entry per file and return them."""
        entries: list[ManifestEntry] = []
        for f in files:
            entries.append(
                ledger.append(
                    self._record(
                        product,
                        request,
                        status=ManifestStatus.fetched,
                        path=f.path,
                        sha256=f.sha256,
                        size_bytes=f.size_bytes,
                        url=f.url if f.url is not None else product.url,
                        params=f.params,
                        notes=f.notes,
                        product_level=f.product_level,
                    )
                )
            )
        return entries

    # -- ledger -----------------------------------------------------------------------------

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _record(
        self,
        product: ProductRecord,
        request: IngestRequest,
        *,
        status: ManifestStatus,
        path: Path | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        url: str | None = None,
        params: dict[str, Any] | None = None,
        notes: str | None = None,
        product_level: str | None = None,
        retention: Retention = Retention.retained,
    ) -> ManifestEntry:
        """Build a `ManifestEntry` for `product`; the only constructor the adapters use."""
        now = self._clock()
        merged_params: dict[str, Any] = {"request": request.params} if request.params else {}
        merged_params.update(params or {})
        return ManifestEntry(
            recorded_at=now,
            source=self.source,
            product_id=product.product_id,
            product_level=product_level or product.product_level or request.product_level,
            aoi_id=request.aoi_id,
            event_id=request.event_id,
            path=self._relative_path(path) if path is not None else None,
            url=url if url is not None else product.url,
            params=merged_params,
            sha256=sha256,
            size_bytes=size_bytes,
            estimated_bytes=product.estimated_bytes,
            retrieved_at=now if status == ManifestStatus.fetched else None,
            licence=product.licence,
            licence_source_url=product.licence_source_url,
            provenance=Provenance.real,
            status=status,
            time_start=product.time_start,
            time_end=product.time_end,
            bbox_4326=product.bbox_4326 or request.bbox_4326,
            retention=retention,
            adapter=self.adapter_name,
            adapter_version=self.adapter_version,
            serac_git_sha=self._git_sha,
            notes=notes,
        )
