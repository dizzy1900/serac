"""Filesystem experiment tracker: JSON under `reports/experiments/<project>/<run_id>.json`."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serac import __version__
from serac.ports.tracker import RunRecord, Scalar, Tracker
from serac.validation.result import git_sha


class LocalTracker(Tracker):
    """Writes runs to disk. No account, no network, safe in tests."""

    def __init__(self, root: Path, repo: Path | None = None) -> None:
        self.root = root
        self.repo = repo
        self._record: RunRecord | None = None
        self._step = 0

    @property
    def record(self) -> RunRecord:
        if self._record is None:
            raise RuntimeError("tracker run not started")
        return self._record

    def path(self) -> Path:
        return self.root / self.record.project / f"{self.record.run_id}.json"

    def start(
        self, *, project: str, name: str, config: dict[str, Any], tags: list[str] | None = None
    ) -> str:
        self._record = RunRecord(
            run_id=uuid.uuid4().hex[:12],
            project=project,
            name=name,
            started_at_utc=datetime.now(tz=UTC),
            config=config,
            tags=list(tags or []),
            git_sha=git_sha(self.repo),
            serac_version=__version__,
        )
        self._step = 0
        self._flush()
        return self._record.run_id

    def log(self, metrics: dict[str, Scalar], *, step: int | None = None) -> None:
        row: dict[str, Any] = {"step": self._step if step is None else step}
        row.update(metrics)
        self.record.metrics.append(row)
        self._step = int(row["step"]) + 1
        self._flush()

    def summarise(self, summary: dict[str, Any]) -> None:
        self.record.summary.update(summary)
        self._flush()

    def finish(self, status: str = "finished") -> None:
        if self._record is None:
            return
        self._record.finished_at_utc = datetime.now(tz=UTC)
        self._record.status = status
        self._flush()

    def _flush(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(json.loads(self.record.model_dump_json()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
