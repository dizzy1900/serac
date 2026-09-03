from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from serac.domain.replay import (
    CONTRACTS,
    DetectorSummary,
    FixtureFile,
    FixtureManifest,
    FixtureRef,
    FixtureRequest,
    ReplayCounts,
    ReplayReport,
    StreamTimeLatencies,
    TimeWindow,
    WallClockLatencies,
)

T0 = datetime(2021, 2, 7, 4, 49, tzinfo=UTC)
T1 = datetime(2021, 2, 7, 4, 57, tzinfo=UTC)
SHA = "e64753b990ec74a96aaac4d50e1947aab5fccad6384f9550c097a84370c3f9dc"


def request() -> FixtureRequest:
    return FixtureRequest(client="EARTHSCOPE", base_url="https://service.earthscope.org")


def file() -> FixtureFile:
    return FixtureFile(path="NK.KKN..BHZ.mseed", kind="miniseed", sha256=SHA, size_bytes=27136)


class TestTimeWindow:
    def test_ordering(self) -> None:
        with pytest.raises(ValidationError, match="after"):
            TimeWindow(start_utc=T1, end_utc=T0)
        with pytest.raises(ValidationError):
            TimeWindow(start_utc=T0, end_utc=T0)


class TestFixtureManifest:
    def test_fetched(self) -> None:
        m = FixtureManifest(
            event_id="chamoli-2021",
            window=TimeWindow(start_utc=T0, end_utc=T1),
            files=[file()],
            request=request(),
            retrieved_at_utc=T1,
            status="fetched",
        )
        assert m.licence is None
        assert FixtureManifest.model_validate_json(m.model_dump_json()) == m

    def test_not_fetched_has_no_files(self) -> None:
        FixtureManifest(
            event_id="x",
            window=TimeWindow(start_utc=T0, end_utc=T1),
            request=request(),
            status="not_fetched",
        )
        with pytest.raises(ValidationError, match="not_fetched"):
            FixtureManifest(
                event_id="x",
                window=TimeWindow(start_utc=T0, end_utc=T1),
                files=[file()],
                request=request(),
                retrieved_at_utc=T1,
                status="not_fetched",
            )

    def test_fetched_needs_files_and_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="retrieved_at_utc"):
            FixtureManifest(
                event_id="x",
                window=TimeWindow(start_utc=T0, end_utc=T1),
                files=[file()],
                request=request(),
                status="fetched",
            )

    def test_partial_lists_missing(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            FixtureManifest(
                event_id="x",
                window=TimeWindow(start_utc=T0, end_utc=T1),
                files=[file()],
                request=request(),
                retrieved_at_utc=T1,
                status="partial",
            )


def report(**overrides: object) -> ReplayReport:
    fields: dict[str, object] = {
        "replay_run_id": "run",
        "event_id": "chamoli-2021",
        "bus": "in_memory",
        "speed": "max",
        "status": "completed",
        "fixtures": [FixtureRef(path="data/fixtures/x.mseed", sha256=SHA, provenance="real")],
        "counts": ReplayCounts(
            chunks_published=96,
            chunks_consumed=96,
            detections_emitted=0,
            cap_messages_emitted=0,
            pending_after_drain=0,
        ),
        "stream_time_latencies": StreamTimeLatencies(),
        "wall_clock_latencies": WallClockLatencies(valid=False, total_run_s=1.5),
        "detector": DetectorSummary(name="lp-sp-ratio-stub", version="0.0.1"),
        "started_at_utc": T0,
        "finished_at_utc": T0 + timedelta(seconds=2),
        "caveats": ["detector is a stub"],
    }
    fields.update(overrides)
    return ReplayReport.model_validate(fields)


class TestReplayReport:
    def test_minimal(self) -> None:
        r = report()
        assert r.is_stub is True
        assert r.contains_synthetic is False
        assert ReplayReport.model_validate_json(r.model_dump_json()) == r

    def test_is_stub_cannot_be_false(self) -> None:
        with pytest.raises(ValidationError):
            report(is_stub=False)

    def test_caveats_required(self) -> None:
        with pytest.raises(ValidationError):
            report(caveats=[])

    def test_wall_clock_only_valid_at_speed_one(self) -> None:
        with pytest.raises(ValidationError, match=r"speed 1\.0"):
            report(wall_clock_latencies=WallClockLatencies(valid=True))
        report(speed=1.0, wall_clock_latencies=WallClockLatencies(valid=True))

    def test_contains_synthetic_tracks_fixtures(self) -> None:
        synthetic = [FixtureRef(path="synthetic-lp-burst", sha256=SHA, provenance="synthetic")]
        with pytest.raises(ValidationError, match="contains_synthetic"):
            report(fixtures=synthetic)
        assert report(fixtures=synthetic, contains_synthetic=True).contains_synthetic

    def test_failed_needs_error(self) -> None:
        with pytest.raises(ValidationError, match="error"):
            report(status="failed")
        report(status="failed", error="boom")

    def test_time_ordering(self) -> None:
        with pytest.raises(ValidationError):
            report(finished_at_utc=T0 - timedelta(seconds=1))


def test_contract_registry() -> None:
    assert {"replay-report": ReplayReport, "fixture-manifest": FixtureManifest} == CONTRACTS
