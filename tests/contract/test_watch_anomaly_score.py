"""The watch-anomaly-score contract is registered and carries no predicted-date field."""

from __future__ import annotations

import json
from pathlib import Path

from serac.domain.schema_export import contract_filename, discover_contracts
from serac.domain.watch_model import CONTRACTS, WATCH_ANOMALY_SCORE_CONTRACT_VERSION, UnitWatchScore


def test_watch_anomaly_score_is_registered() -> None:
    assert {"watch-anomaly-score": UnitWatchScore} == CONTRACTS
    registered = discover_contracts()
    assert registered["watch-anomaly-score"] is UnitWatchScore


def test_watch_anomaly_score_schema_has_no_predicted_date_fields(repo_root: Path) -> None:
    path = repo_root / "contracts" / contract_filename("watch-anomaly-score")
    schema = json.loads(path.read_text(encoding="utf-8"))
    names = set(schema["properties"])
    banned = {
        "failure_date",
        "failure_time_predicted",
        "predicted_failure",
        "time_to_failure",
        "days_to_failure",
        "failure_probability",
        "probability_of_failure",
        "p_failure",
    }
    assert names.isdisjoint(banned)
    assert schema["properties"]["contract_version"]["default"] == (
        WATCH_ANOMALY_SCORE_CONTRACT_VERSION
    )
    assert "score" in names and "max_input_time" in names
    score_desc = schema["properties"]["score"].get("description", "")
    assert "NOT a probability" in score_desc
