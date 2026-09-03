from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from serac.domain.seismic import (
    CONTRACTS,
    SeismicTrace,
    Sncl,
    TraceEncoding,
    TraceProvenance,
    TraceSource,
    sha256_of_bytes,
)

T0 = datetime(2021, 2, 7, 4, 51, tzinfo=UTC)
SYNTHETIC = TraceProvenance(source=TraceSource.synthetic, notes="unit test placeholder")


def make_trace(npts: int = 100, rate: float = 50.0, **overrides: object) -> SeismicTrace:
    data = b"\x00\x00\x80\x3f" * npts
    fields: dict[str, object] = {
        "trace_id": "NK.KKN..BHZ/test/0",
        "sncl": Sncl.from_key("NK.KKN..BHZ"),
        "start_time_utc": T0,
        "end_time_utc": T0 + timedelta(seconds=(npts - 1) / rate),
        "sampling_rate_hz": rate,
        "npts": npts,
        "encoding": TraceEncoding.float32le,
        "data": data,
        "data_sha256": sha256_of_bytes(data),
        "sequence": 0,
        "provenance": SYNTHETIC,
    }
    fields.update(overrides)
    return SeismicTrace.model_validate(fields)


class TestSncl:
    def test_key_round_trip(self) -> None:
        assert Sncl.from_key("IC.LSA.00.BHZ").key == "IC.LSA.00.BHZ"
        assert Sncl.from_key("NK.KKN..BHZ").location == ""
        assert Sncl.from_key("NK.KKN..BHZ").key == "NK.KKN..BHZ"

    @pytest.mark.parametrize("bad", ["NK.KKN.BHZ", "nk.kkn..bhz", "NK.KKN..BHZZ", ""])
    def test_rejects_malformed_keys(self, bad: str) -> None:
        with pytest.raises(ValueError):
            Sncl.from_key(bad)

    def test_is_frozen(self) -> None:
        sncl = Sncl.from_key("NK.KKN..BHZ")
        with pytest.raises(ValidationError):
            sncl.station = "X"  # type: ignore[misc]


class TestProvenance:
    def test_synthetic_requires_notes(self) -> None:
        with pytest.raises(ValidationError, match="notes"):
            TraceProvenance(source=TraceSource.synthetic)
        with pytest.raises(ValidationError, match="notes"):
            TraceProvenance(source=TraceSource.synthetic, notes="   ")

    def test_fixture_requires_path(self) -> None:
        with pytest.raises(ValidationError, match="fixture_path"):
            TraceProvenance(source=TraceSource.fixture)
        TraceProvenance(source=TraceSource.fixture, fixture_path="data/fixtures/x.mseed")

    def test_licence_may_be_null(self) -> None:
        prov = TraceProvenance(
            source=TraceSource.fdsn,
            server="https://service.earthscope.org",
            licence_source_url="https://www.earthscope.org/terms-of-service/",
        )
        assert prov.licence is None


class TestSeismicTrace:
    def test_valid(self) -> None:
        trace = make_trace()
        assert trace.duration_s == pytest.approx(99 / 50)
        assert trace.is_synthetic
        assert trace.units == "counts"

    def test_end_time_must_match_npts_and_rate(self) -> None:
        with pytest.raises(ValidationError, match="inconsistent"):
            make_trace(end_time_utc=T0 + timedelta(seconds=5))

    def test_end_time_tolerates_one_sample(self) -> None:
        make_trace(end_time_utc=T0 + timedelta(seconds=99 / 50 + 0.015))
        with pytest.raises(ValidationError):
            make_trace(end_time_utc=T0 + timedelta(seconds=99 / 50 + 0.025))

    def test_sha256_must_match(self) -> None:
        with pytest.raises(ValidationError, match="data_sha256"):
            make_trace(data_sha256="0" * 64)

    def test_float32_length_must_match_npts(self) -> None:
        data = b"\x00\x00\x80\x3f" * 100
        with pytest.raises(ValidationError, match=r"4\*npts"):
            make_trace(
                npts=99,
                end_time_utc=T0 + timedelta(seconds=98 / 50),
                data=data,
                data_sha256=sha256_of_bytes(data),
            )

    def test_miniseed_length_is_not_constrained(self) -> None:
        data = b"\x01\x02\x03"
        make_trace(encoding=TraceEncoding.miniseed, data=data, data_sha256=sha256_of_bytes(data))

    def test_json_round_trip_keeps_bytes(self) -> None:
        trace = make_trace()
        raw = trace.model_dump_json()
        assert '"data":"' in raw
        again = SeismicTrace.model_validate_json(raw)
        assert again == trace
        assert again.data == trace.data

    def test_rejects_extra_fields_and_naive_times(self) -> None:
        with pytest.raises(ValidationError):
            make_trace(extra="nope")
        with pytest.raises(ValidationError):
            make_trace(start_time_utc=T0.replace(tzinfo=None))

    def test_quality_pattern(self) -> None:
        make_trace(quality="D")
        with pytest.raises(ValidationError):
            make_trace(quality="X")

    def test_contract_registry(self) -> None:
        assert {"seismic-trace": SeismicTrace} == CONTRACTS
