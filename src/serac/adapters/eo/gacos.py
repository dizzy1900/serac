"""GACOS tropospheric corrections: a request/poll/receive workflow, not a download API.

GACOS (Generic Atmospheric Correction Online Service for InSAR, Newcastle University) takes a
web-form request (bounding box, acquisition dates, UTC time, e-mail) and later e-mails a link
to an archive of zenith total delay maps. There is no query API, so serac models the
workflow explicitly:

1. `request()` validates the form values, assigns a `request_id`, records
   `status: requested` in the provenance ledger (the form values live in `params`) and, when a
   form endpoint is configured, POSTs the form through `GacosFormClient`. By default the
   endpoint is `None`: the operator submits the printed values on the GACOS site by hand and
   the ledger row is the receipt.
2. `poll()` reports the ledger state of a request and, if a delivery URL is already known,
   whether it answers a HEAD.
3. `receive()` (`serac ingest gacos --receive URL --request-id ID`) downloads the e-mailed
   archive, hashes it, and appends a `fetched` row that references the request.

Nothing here fabricates a delay map; without a delivered link the dataset stays `requested`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

from serac.adapters.eo._base import BaseIngestAdapter, FetchedFile, product_dir
from serac.adapters.eo._http import HttpClient, HttpxClient
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus
from serac.errors import CredentialsMissingError
from serac.ports.ingest import CredentialSpec, DryRunPlan, IngestRequest, ProductRecord
from serac.ports.ledger import ManifestLedger

GACOS_SITE_URL = "http://www.gacos.net/"
GACOS_LICENCE = (
    "GACOS products are provided free of charge for research; users are asked to cite the "
    "GACOS papers and acknowledge the service as stated on the GACOS site"
)
GACOS_LICENCE_URL = GACOS_SITE_URL
GACOS_EMAIL_CREDENTIAL = CredentialSpec(
    name="GACOS delivery e-mail",
    env_vars=("GACOS_EMAIL",),
    purpose="the address GACOS e-mails the correction archive to",
)
ARCHIVE_FILENAME = "gacos.tar.gz"


class GacosFormClient(Protocol):
    """POST the request form; returns the service's textual acknowledgement."""

    def post_form(self, url: str, data: dict[str, str]) -> str: ...


@dataclass(frozen=True)
class GacosRequestForm:
    """Exactly the fields the GACOS form asks for."""

    north: float
    south: float
    west: float
    east: float
    dates: tuple[str, ...]
    time_utc: str
    email: str

    def as_form(self) -> dict[str, str]:
        return {
            "N": f"{self.north:.4f}",
            "S": f"{self.south:.4f}",
            "W": f"{self.west:.4f}",
            "E": f"{self.east:.4f}",
            "date": ",".join(self.dates),
            "time": self.time_utc,
            "email": self.email,
        }


def _utc_hhmm(text: str) -> str:
    hh, mm = text.split(":")[:2]
    if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
        raise ValueError(f"time_utc must be HH:MM, got {text!r}")
    return f"{int(hh):02d}:{int(mm):02d}"


def build_form(request: IngestRequest, email: str) -> GacosRequestForm:
    dates_raw = request.params.get("dates")
    if not dates_raw:
        raise ValueError("GACOS needs params['dates'] (YYYYMMDD strings of the SAR acquisitions)")
    dates = tuple(str(d) for d in dates_raw)
    for d in dates:
        datetime.strptime(d, "%Y%m%d")  # a bare date token; no zone applies
    time_utc = _utc_hhmm(str(request.params.get("time_utc", "00:00")))
    w, s, e, n = request.bbox_4326
    return GacosRequestForm(
        north=n, south=s, west=w, east=e, dates=dates, time_utc=time_utc, email=email
    )


class GacosAdapter(BaseIngestAdapter):
    """Request / poll / receive; see the module docstring."""

    source: ClassVar[DataSource] = DataSource.gacos
    adapter_name: ClassVar[str] = "gacos"
    adapter_version: ClassVar[str] = "0.1.0"
    licence: ClassVar[str] = GACOS_LICENCE
    licence_source_url: ClassVar[str | None] = GACOS_LICENCE_URL
    credentials: ClassVar[tuple[CredentialSpec, ...]] = (GACOS_EMAIL_CREDENTIAL,)

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        form_client: GacosFormClient | None = None,
        form_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._http = http
        self.form_client = form_client
        self.form_url = form_url

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpxClient()
        return self._http

    # -- port: the request is the product ------------------------------------------------------

    @staticmethod
    def product_id(request: IngestRequest) -> str:
        dates = [str(d) for d in request.params.get("dates", [])]
        span = f"{dates[0]}_{dates[-1]}" if dates else "undated"
        return f"gacos_ztd_{request.aoi_id}_{span}"

    def search(self, request: IngestRequest) -> list[ProductRecord]:
        email = self.settings.gacos_email or "<GACOS_EMAIL unset>"
        form = build_form(request, email)
        return [
            ProductRecord(
                source=self.source,
                product_id=self.product_id(request),
                product_level="ZTD",
                url=GACOS_SITE_URL,
                bbox_4326=request.bbox_4326,
                estimated_bytes=None,
                licence=self.licence,
                licence_source_url=self.licence_source_url,
                properties={
                    "form": {**form.as_form(), "email": "<redacted>"},
                    "n_dates": len(form.dates),
                },
            )
        ]

    def plan(self, request: IngestRequest) -> DryRunPlan:
        products = self.search(request)
        return self.build_plan(
            request,
            products,
            estimated_bytes=None,
            estimate_basis=(
                "GACOS delivers an archive by e-mail after a human-in-the-loop request; the "
                "size is unknown until the link arrives"
            ),
            warnings=[
                "fetch() records the request only; run `serac ingest gacos --receive URL "
                "--request-id ID` when the e-mail arrives"
            ],
        )

    # -- request ------------------------------------------------------------------------------

    def request(
        self, plan: DryRunPlan, *, ledger: ManifestLedger, request_id: str | None = None
    ) -> ManifestEntry:
        """Record `requested` (and POST the form when an endpoint is configured)."""
        email = self.settings.gacos_email
        product = plan.products[0]
        if email is None:
            ledger.append(
                self._record(
                    product,
                    plan.request,
                    status=ManifestStatus.not_fetched,
                    notes=(
                        "credentials missing: GACOS delivery e-mail (GACOS_EMAIL); "
                        "see docs/CREDENTIALS.md"
                    ),
                )
            )
            raise CredentialsMissingError(
                "gacos needs GACOS_EMAIL; recorded not_fetched. See docs/CREDENTIALS.md."
            )
        form = build_form(plan.request, email)
        rid = request_id or uuid.uuid4().hex[:12]
        acknowledgement: str | None = None
        if self.form_url is not None and self.form_client is not None:
            acknowledgement = self.form_client.post_form(self.form_url, form.as_form())
        return ledger.append(
            self._record(
                product,
                plan.request,
                status=ManifestStatus.requested,
                params={
                    "request_id": rid,
                    "form": {**form.as_form(), "email": "<redacted>"},
                    "form_url": self.form_url,
                    "acknowledgement": (acknowledgement or "")[:500] or None,
                },
                notes=(
                    "GACOS request recorded; submitted "
                    + (
                        "through the configured form endpoint"
                        if acknowledgement is not None
                        else "by hand on the GACOS site"
                    )
                    + "; complete with `serac ingest gacos --receive URL --request-id "
                    + rid
                    + "`"
                ),
            )
        )

    def fetch(
        self, plan: DryRunPlan, *, dest_root: Path, ledger: ManifestLedger, confirm: Any
    ) -> list[ManifestEntry]:
        """`fetch` on GACOS means `request`; bytes arrive later through `receive`."""
        return [self.request(plan, ledger=ledger)]

    # -- poll / receive -----------------------------------------------------------------------

    def find_request(self, ledger: ManifestLedger, request_id: str) -> ManifestEntry | None:
        rows = [
            e
            for e in ledger.entries()
            if e.source == self.source and e.params.get("request_id") == request_id
        ]
        return max(rows, key=lambda e: e.recorded_at) if rows else None

    def poll(
        self, ledger: ManifestLedger, request_id: str, *, receive_url: str | None = None
    ) -> dict[str, Any]:
        """State of a request: `requested` / `fetched` / `unknown`, plus link availability."""
        entry = self.find_request(ledger, request_id)
        if entry is None:
            return {"request_id": request_id, "state": "unknown"}
        state: dict[str, Any] = {
            "request_id": request_id,
            "state": entry.status.value,
            "recorded_at": entry.recorded_at.isoformat(),
            "product_id": entry.product_id,
        }
        if receive_url is not None:
            try:
                state["link_content_length"] = self.http.head_content_length(receive_url)
                state["link_available"] = True
            except Exception as exc:  # the link is not there yet: say so, do not guess
                state["link_available"] = False
                state["link_error"] = f"{type(exc).__name__}: {exc}"
        return state

    def receive(
        self, ledger: ManifestLedger, *, request_id: str, url: str, dest_root: Path
    ) -> ManifestEntry:
        """Download the delivered archive and append the `fetched` row for the request."""
        requested = self.find_request(ledger, request_id)
        if requested is None:
            raise ValueError(f"no GACOS request with request_id={request_id!r} in the ledger")
        aoi_id = requested.aoi_id or "unknown-aoi"
        dest = product_dir(dest_root, self.source, aoi_id, requested.product_id)
        out = dest / ARCHIVE_FILENAME
        sha, size = self.http.stream_to(url, out)
        product = ProductRecord(
            source=self.source,
            product_id=requested.product_id,
            product_level="ZTD",
            url=url,
            bbox_4326=requested.bbox_4326,
            licence=self.licence,
            licence_source_url=self.licence_source_url,
        )
        request = IngestRequest(
            aoi_id=aoi_id, bbox_4326=requested.bbox_4326 or (0.0, 0.0, 0.0, 0.0)
        )
        file = FetchedFile(
            path=out,
            sha256=sha,
            size_bytes=size,
            url=url,
            params={
                "request_id": request_id,
                "requested_entry_id": requested.entry_id,
                "form": requested.params.get("form"),
            },
            notes="GACOS archive downloaded from the e-mailed delivery link",
            product_level="ZTD",
        )
        return self.record_files(product, request, [file], ledger)[0]

    def _fetch_product(
        self, product: ProductRecord, dest: Path, request: IngestRequest
    ) -> list[FetchedFile]:  # pragma: no cover - fetch() is overridden; kept for the ABC
        raise NotImplementedError("GacosAdapter.fetch records a request; use receive()")
