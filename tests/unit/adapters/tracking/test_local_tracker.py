from __future__ import annotations

import json
from pathlib import Path

import pytest

from serac.adapters.tracking.local import LocalTracker


def test_run_is_written_and_readable(tmp_path: Path) -> None:
    tracker = LocalTracker(tmp_path)
    run_id = tracker.start(project="m1", name="baseline", config={"seed": 1}, tags=["test"])
    tracker.log({"f1": 0.5})
    tracker.log({"f1": 0.7})
    tracker.summarise({"best_f1": 0.7})
    tracker.finish()
    doc = json.loads((tmp_path / "m1" / f"{run_id}.json").read_text())
    assert doc["status"] == "finished"
    assert [row["step"] for row in doc["metrics"]] == [0, 1]
    assert doc["summary"]["best_f1"] == 0.7
    assert doc["config"] == {"seed": 1}


def test_context_manager_records_failure(tmp_path: Path) -> None:
    tracker = LocalTracker(tmp_path)
    tracker.start(project="m1", name="boom", config={}, tags=[])
    with pytest.raises(RuntimeError), tracker:
        raise RuntimeError("boom")
    assert json.loads(tracker.path().read_text())["status"] == "failed"


def test_logging_before_start_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not started"):
        LocalTracker(tmp_path).log({"x": 1.0})
