from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from serac.domain.detection import CONTRACTS, DetectionCandidate
from serac.domain.force_history import CONTRACTS as FORCE_CONTRACTS
from serac.domain.force_history import ForceHistory
from serac.domain.seismic import Sncl

T0 = datetime(2021, 2, 7, 4, 51, tzinfo=UTC)


def make(**overrides: object) -> DetectionCandidate:
    fields: dict[str, object] = {
        "detection_id": "d",
        "sncl": Sncl.from_key("NK.KKN..BHZ"),
        "detector": "lp-sp-ratio-stub",
        "detector_version": "0.0.1",
        "window_start_utc": T0,
        "window_end_utc": T0 + timedelta(seconds=120),
        "detected_at_stream_utc": T0 + timedelta(seconds=120),
        "score": 11.0,
        "threshold": 10.0,
    }
    fields.update(overrides)
    return DetectionCandidate.model_validate(fields)


def test_defaults_mark_the_stub() -> None:
    det = make()
    assert det.is_stub is True
    assert det.source_location is None
    assert det.features == {}
    assert det.input_trace_ids == []


def test_source_location_can_only_be_none() -> None:
    with pytest.raises(ValidationError, match="source_location"):
        make(source_location={"lat": 28.27, "lon": 85.51})
    with pytest.raises(ValidationError, match="source_location"):
        make(source_location="28.27,85.51")


def test_window_ordering() -> None:
    with pytest.raises(ValidationError, match="window_end_utc"):
        make(window_end_utc=T0 - timedelta(seconds=1), detected_at_stream_utc=T0)


def test_detection_time_inside_window() -> None:
    with pytest.raises(ValidationError, match="within the window"):
        make(detected_at_stream_utc=T0 + timedelta(seconds=121))
    make(detected_at_stream_utc=T0)


def test_json_round_trip() -> None:
    det = make(features={"lp_energy": 1.5, "sp_energy": 0.1}, input_trace_ids=["a", "b"])
    assert DetectionCandidate.model_validate_json(det.model_dump_json()) == det


def test_contract_registry() -> None:
    assert {"detection-candidate": DetectionCandidate} == CONTRACTS
    assert {"force-history": ForceHistory} == FORCE_CONTRACTS


class TestForceHistory:
    def test_default_is_not_implemented(self) -> None:
        fh = ForceHistory()
        assert fh.status == "not_implemented"
        assert fh.force_up_n is None
        assert "not implemented" in fh.notes

    def test_status_literal_is_closed(self) -> None:
        with pytest.raises(ValidationError):
            ForceHistory(status="computed")  # type: ignore[arg-type]

    def test_samples_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ForceHistory(force_up_n=[1.0, 2.0])  # type: ignore[arg-type]
