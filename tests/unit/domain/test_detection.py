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


def test_defaults_are_not_a_stub_and_carry_no_location() -> None:
    # 0.2.0 flipped the default: a stub must now say so explicitly.
    det = make()
    assert det.is_stub is False
    assert det.source_location is None
    assert det.features == {}
    assert det.input_trace_ids == []


def test_source_location_needs_a_real_inversion() -> None:
    # Coordinates alone are not a location: the method, grid spacing, variance reduction
    # and azimuthal gap of the inversion that produced it are all required.
    with pytest.raises(ValidationError):
        make(source_location={"latitude": 20.5, "longitude": 10.5})
    located = make(
        source_location={
            "latitude": 20.5,
            "longitude": 10.5,
            "method": "gsf_grid_search",
            "grid_spacing_km": 11.0,
            "variance_reduction": 0.72,
            "azimuthal_gap_deg": 140.0,
        }
    )
    assert located.source_location is not None


def test_a_stub_may_not_attach_a_location() -> None:
    with pytest.raises(ValidationError, match="stub detector must not attach"):
        make(
            is_stub=True,
            source_location={
                "latitude": 20.5,
                "longitude": 10.5,
                "method": "gsf_grid_search",
                "grid_spacing_km": 11.0,
                "variance_reduction": 0.72,
                "azimuthal_gap_deg": 140.0,
            },
        )


def test_probability_requires_its_calibration() -> None:
    with pytest.raises(ValidationError, match="probability requires"):
        make(probability=0.9)
    assert make(probability=0.9, probability_calibration="sigmoid").probability == 0.9
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
    assert CONTRACTS["detection-candidate"] is DetectionCandidate
    assert set(CONTRACTS) == {"detection-candidate", "detection-location"}
    assert FORCE_CONTRACTS["force-history"] is ForceHistory
    assert set(FORCE_CONTRACTS) == {"force-history", "mass-estimate"}


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
