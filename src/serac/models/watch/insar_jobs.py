"""Drive the HyP3 burst-InSAR network: submit, poll, harvest, crop, delete — resumably.

A five-year SBAS network is a few hundred jobs that take hours to process, so every step is
restartable from a file. `jobs.jsonl` is append-only, latest row per pair wins, and both
`submit_network` and `poll_and_harvest` are safe to run repeatedly: submission skips pairs
that already have a job, harvesting skips pairs whose crops are already on disk.

The disk discipline is the point of `poll_and_harvest`: one product is on disk at a time. A
zip arrives, is hashed, is cropped to the AOI grid, and is deleted before the next is fetched,
so peak usage is one zip plus the accumulated crops rather than the whole network.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.adapters.eo.hyp3_burst import (
    HYP3_LICENCE,
    TERMINAL_STATUSES,
    Hyp3BurstInsarAdapter,
    Hyp3Error,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource
from serac.models.watch.crop import make_cropper
from serac.models.watch.plan import NetworkPlan, load_network_plan
from serac.ports.ingest import IngestRequest, ProductRecord

JOBS_FILENAME = "jobs.jsonl"


class PairJob(BaseModel):
    """One row of `data/raw/hyp3_burst_insar/<aoi>/jobs.jsonl`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    job_id: str
    job_name: str
    status: str
    submitted_at: AwareDatetime
    updated_at: AwareDatetime
    harvested: bool = False
    zip_bytes: int | None = None
    zip_sha256: str | None = None
    crop_paths: list[str] = Field(default_factory=list)
    manifest_entry_ids: list[str] = Field(default_factory=list)
    failure: str | None = None


def jobs_ledger_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "raw" / "hyp3_burst_insar" / aoi_id / JOBS_FILENAME


def read_jobs(path: Path) -> list[PairJob]:
    if not path.exists():
        return []
    return [
        PairJob.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def latest_by_pair(path: Path) -> dict[str, PairJob]:
    out: dict[str, PairJob] = {}
    for row in read_jobs(path):
        prev = out.get(row.pair_id)
        if prev is None or row.updated_at >= prev.updated_at:
            out[row.pair_id] = row
    return out


def append_job(path: Path, job: PairJob) -> PairJob:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(job.model_dump_json() + "\n")
    return job


def _pair_product(plan: NetworkPlan, pair: Any) -> ProductRecord:
    return ProductRecord(
        source=DataSource.hyp3_insar,
        product_id=f"S1_{plan.path_number:03d}_{pair.pair_id}",
        product_level="INSAR_ISCE_MULTI_BURST",
        time_start=pair.reference_date,
        time_end=pair.secondary_date,
        licence=HYP3_LICENCE,
        properties={
            "reference": list(pair.reference_granules),
            "secondary": list(pair.secondary_granules),
            "pathNumber": plan.path_number,
            "temporal_baseline_days": pair.temporal_baseline_days,
            "kind": pair.kind,
            "looks": plan.looks,
        },
    )


def _request(plan: NetworkPlan) -> IngestRequest:
    bbox = plan.crop_grid
    return IngestRequest(
        aoi_id=plan.aoi_id,
        bbox_4326=_grid_bbox_4326(plan),
        time_start=plan.window_start,
        time_end=plan.window_end,
        product_level="INSAR_ISCE_MULTI_BURST",
        params={
            "path_number": plan.path_number,
            "looks": plan.looks,
            "burst_ids": plan.burst_ids,
            "n_conn": plan.n_conn,
            "max_bt_days": plan.max_bt_days,
            "plan_sha256": plan.plan_sha256,
            "crop_grid": bbox,
        },
    )


def _grid_bbox_4326(plan: NetworkPlan) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    grid = plan.watch_grid
    transformer = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    x0, y0, x1, y1 = grid.bounds
    xs, ys = transformer.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    return (min(xs), min(ys), max(xs), max(ys))


def submit_network(
    *,
    data_dir: Path,
    aoi_id: str,
    limit: int | None = None,
    dry_run: bool = True,
    adapter: Hyp3BurstInsarAdapter | None = None,
) -> dict[str, Any]:
    """Submit every pair that has no job yet. Idempotent; `--limit` throttles a first batch."""
    plan = load_network_plan(data_dir, aoi_id)
    jobs_path = jobs_ledger_path(data_dir, aoi_id)
    known = latest_by_pair(jobs_path)
    todo = [p for p in plan.pairs if p.pair_id not in known]
    if limit is not None:
        todo = todo[:limit]
    summary: dict[str, Any] = {
        "aoi_id": aoi_id,
        "path_number": plan.path_number,
        "plan_sha256": plan.plan_sha256,
        "pairs_total": len(plan.pairs),
        "already_submitted": len(known),
        "to_submit": len(todo),
        "credits_required": len(todo) * plan.budget.credits_per_job,
        "submitted": 0,
        "errors": [],
    }
    if dry_run:
        summary["note"] = "dry run: pass --yes to submit"
        return summary

    eo = adapter or Hyp3BurstInsarAdapter(repo_root=data_dir.resolve().parent)
    ledger = JsonlManifestLedger(data_dir / "manifest.jsonl")
    request = _request(plan)
    for pair in todo:
        product = _pair_product(plan, pair)
        job_name = f"serac-{aoi_id}-{plan.path_number:03d}-{pair.pair_id}"[:100]
        now = datetime.now(tz=UTC)
        try:
            job = eo.submit_pair(
                pair_id=pair.pair_id,
                reference=pair.reference_granules,
                secondary=pair.secondary_granules,
                looks=plan.looks,
                job_name=job_name,
            )
        except Hyp3Error as exc:
            summary["errors"].append({"pair_id": pair.pair_id, "error": str(exc)})
            continue
        entry = eo.record_submission(job=job, pair=product, request=request, ledger=ledger)
        append_job(
            jobs_path,
            PairJob(
                pair_id=pair.pair_id,
                job_id=job.job_id,
                job_name=job_name,
                status=job.status,
                submitted_at=now,
                updated_at=now,
                manifest_entry_ids=[entry.entry_id],
            ),
        )
        summary["submitted"] = int(summary["submitted"]) + 1
    return summary


def poll_and_harvest(
    *,
    data_dir: Path,
    reports_dir: Path,
    aoi_id: str,
    once: bool = False,
    poll_s: float = 120.0,
    timeout_s: float = 6 * 3600,
    adapter: Hyp3BurstInsarAdapter | None = None,
) -> dict[str, Any]:
    """Poll HyP3 and harvest what has succeeded, one product on disk at a time."""
    plan = load_network_plan(data_dir, aoi_id)
    jobs_path = jobs_ledger_path(data_dir, aoi_id)
    eo = adapter or Hyp3BurstInsarAdapter(repo_root=data_dir.resolve().parent)
    ledger = JsonlManifestLedger(data_dir / "manifest.jsonl")
    request = _request(plan)
    cropper = make_cropper(plan.watch_grid)
    by_pair = {p.pair_id: p for p in plan.pairs}
    deadline = time.monotonic() + timeout_s
    summary: dict[str, Any] = {
        "aoi_id": aoi_id,
        "harvested": 0,
        "failed": 0,
        "pending": 0,
        "bytes_transient": 0,
        "bytes_retained": 0,
        "errors": [],
    }

    while True:
        known = latest_by_pair(jobs_path)
        pending = [j for j in known.values() if not j.harvested and j.failure is None]
        summary["pending"] = len(pending)
        progressed = False
        for row in sorted(pending, key=lambda j: j.pair_id):
            pair = by_pair.get(row.pair_id)
            if pair is None:
                continue
            try:
                job = eo.client.get_job(row.job_id)
            except Hyp3Error as exc:
                summary["errors"].append({"pair_id": row.pair_id, "error": str(exc)})
                continue
            now = datetime.now(tz=UTC)
            if job.status == "FAILED":
                append_job(
                    jobs_path,
                    row.model_copy(
                        update={"status": job.status, "updated_at": now, "failure": "HyP3 FAILED"}
                    ),
                )
                summary["failed"] = int(summary["failed"]) + 1
                progressed = True
                continue
            if job.status != "SUCCEEDED":
                continue
            product = _pair_product(plan, pair)
            try:
                entries = eo.harvest(
                    job=job,
                    pair=product,
                    request=request,
                    ledger=ledger,
                    dest_root=data_dir,
                    crop=cropper,
                )
            except Exception as exc:
                summary["errors"].append(
                    {"pair_id": row.pair_id, "error": f"{type(exc).__name__}: {exc}"}
                )
                append_job(
                    jobs_path,
                    row.model_copy(
                        update={
                            "status": job.status,
                            "updated_at": now,
                            "failure": f"harvest failed: {type(exc).__name__}",
                        }
                    ),
                )
                summary["failed"] = int(summary["failed"]) + 1
                progressed = True
                continue
            zip_entry = entries[0]
            crops = [e for e in entries[1:]]
            append_job(
                jobs_path,
                row.model_copy(
                    update={
                        "status": job.status,
                        "updated_at": now,
                        "harvested": True,
                        "zip_bytes": zip_entry.size_bytes,
                        "zip_sha256": zip_entry.sha256,
                        "crop_paths": [e.path for e in crops if e.path],
                        "manifest_entry_ids": [
                            *row.manifest_entry_ids,
                            *(e.entry_id for e in entries),
                        ],
                    }
                ),
            )
            summary["harvested"] = int(summary["harvested"]) + 1
            summary["bytes_transient"] = int(summary["bytes_transient"]) + (
                zip_entry.size_bytes or 0
            )
            summary["bytes_retained"] = int(summary["bytes_retained"]) + sum(
                e.size_bytes or 0 for e in crops
            )
            progressed = True

        known = latest_by_pair(jobs_path)
        outstanding = [j for j in known.values() if not j.harvested and j.failure is None]
        summary["pending"] = len(outstanding)
        if once or not outstanding or time.monotonic() > deadline:
            break
        if not progressed:
            time.sleep(poll_s)

    summary["submitted_total"] = len(latest_by_pair(jobs_path))
    _write_status(reports_dir, aoi_id, plan, jobs_path, summary)
    return summary


def _write_status(
    reports_dir: Path, aoi_id: str, plan: NetworkPlan, jobs_path: Path, summary: dict[str, Any]
) -> Path:
    rows = latest_by_pair(jobs_path)
    payload = {
        "aoi_id": aoi_id,
        "path_number": plan.path_number,
        "plan_sha256": plan.plan_sha256,
        "pairs_planned": len(plan.pairs),
        "jobs_submitted": len(rows),
        "succeeded_harvested": sum(1 for r in rows.values() if r.harvested),
        "failed": sum(1 for r in rows.values() if r.failure),
        "pending": sum(1 for r in rows.values() if not r.harvested and not r.failure),
        "bytes_transient_total": sum(r.zip_bytes or 0 for r in rows.values()),
        "credits_spent": len(rows) * plan.budget.credits_per_job,
        "last_run": summary,
    }
    out = reports_dir / "watch" / f"insar_jobs_{aoi_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def harvested_pairs(data_dir: Path, aoi_id: str) -> Iterator[PairJob]:
    """Every pair whose crops are on disk, in pair order."""
    rows = latest_by_pair(jobs_ledger_path(data_dir, aoi_id))
    for pair_id in sorted(rows):
        row = rows[pair_id]
        if row.harvested:
            yield row


__all__ = [
    "TERMINAL_STATUSES",
    "PairJob",
    "harvested_pairs",
    "jobs_ledger_path",
    "latest_by_pair",
    "poll_and_harvest",
    "read_jobs",
    "submit_network",
]
