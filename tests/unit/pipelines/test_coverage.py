"""Coverage matrix on fictional records and a fictional ledger. Nothing here is real."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.manifest import DataSource, ManifestStatus
from serac.pipelines.coverage import (
    COLUMNS,
    WINDOWS,
    CoverageReport,
    Window,
    build_report,
    column_matches,
    render_json,
    render_markdown,
    render_table,
    windows_for,
)

if TYPE_CHECKING:
    from tests.unit.conftest import Fictional

T = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def report(tmp_path: Path, fictional: Fictional) -> CoverageReport:
    events_dir = tmp_path / "events"
    fictional.write(
        events_dir,
        fictional.event("test-event-1", time=T, aoi_id="test-aoi", sha256=fictional.sha_a),
        # Dated before NISAR's availability; its source sha256 is deliberately not ledgered.
        fictional.event("test-event-2", time=datetime(2025, 9, 15, tzinfo=UTC), sha256="b" * 64),
        # Before Sentinel-1 and Sentinel-2 existed.
        fictional.event("test-event-3", time=datetime(2014, 1, 1, tzinfo=UTC)),
    )
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    row = fictional.ledger_row
    rows = [
        row(sha256=fictional.sha_a),  # the source document for records 1 and 3
        row(
            DataSource.sentinel1_asf,
            ManifestStatus.listed,
            product_level="SLC",
            time_start=T - 30 * DAY,
            time_end=T - 29 * DAY,
        ),
        row(
            DataSource.sentinel1_asf,
            ManifestStatus.fetched,
            event_id=None,
            aoi_id="test-aoi",
            product_level=None,
            time_start=T,  # [t, t] sits in `event` only: `pre` and `post` exclude t itself
            time_end=T,
        ),
        row(
            DataSource.fdsn_waveforms,
            ManifestStatus.fetched,
            time_start=T - timedelta(minutes=5),
            time_end=T + timedelta(minutes=5),
        ),
        row(
            DataSource.seedlink,
            ManifestStatus.failed,
            time_start=T - timedelta(minutes=5),
            time_end=T + timedelta(minutes=5),
        ),
        row(DataSource.usgs_comcat, ManifestStatus.listed),  # undated, by event_id
        row(DataSource.dem_glo30, ManifestStatus.fetched, event_id=None, aoi_id="test-aoi"),
        row(
            DataSource.era5_cds,
            ManifestStatus.requested,
            time_start=T + 10 * DAY,
            time_end=T + 11 * DAY,
        ),
        row(
            DataSource.era5_cds,
            ManifestStatus.not_fetched,
            time_start=T + 91 * DAY,
            time_end=T + 92 * DAY,
        ),
        row(
            DataSource.sentinel2_cdse,
            ManifestStatus.listed,
            event_id="test-event-2",
            time_start=datetime(2025, 9, 15, tzinfo=UTC),
            time_end=datetime(2025, 9, 15, tzinfo=UTC),
        ),
    ]
    for entry in rows:
        ledger.append(entry)
    ledger.append(rows[3])  # the same entry_id twice must count once
    return build_report(events_dir, ledger, now=T)


def _cells(report: CoverageReport, event_id: str, column: str) -> list[str]:
    row = next(r for r in report.rows if r.event_id == event_id)
    return [row.cell(column, window).text for window in report.windows]


def test_report_shape(report: CoverageReport) -> None:
    assert report.columns == COLUMNS
    assert report.windows == WINDOWS
    assert [r.event_id for r in report.rows] == ["test-event-1", "test-event-2", "test-event-3"]
    assert report.generated_utc == T


def test_cells_by_column_and_window(report: CoverageReport) -> None:
    assert _cells(report, "test-event-1", "s1_slc") == ["listed(1)", "fetched(1)", "-"]
    assert _cells(report, "test-event-1", "s1_grd") == ["-", "fetched(1)", "-"], (
        "product_level=None counts under both; the SLC row only under s1_slc"
    )
    assert _cells(report, "test-event-1", "fdsn_waveforms") == ["fetched(2)"] * 3, (
        "a span straddling t touches all three windows; fetched outranks failed; "
        "the duplicated entry_id is counted once"
    )
    assert _cells(report, "test-event-1", "usgs_comcat") == ["listed(1)"] * 3, (
        "an undated row matched by event_id counts in every window"
    )
    assert _cells(report, "test-event-1", "dem_glo30") == ["-"] * 3, (
        "an undated row matched only by aoi_id is not counted"
    )
    assert _cells(report, "test-event-1", "era5") == ["-", "-", "requested(1)"], (
        "the row at t+91d falls outside the post window"
    )
    assert _cells(report, "test-event-1", "hyp3_insar") == ["-"] * 3
    assert _cells(report, "test-event-1", "nisar") == ["-"] * 3


def test_not_applicable_rules(report: CoverageReport) -> None:
    assert _cells(report, "test-event-2", "nisar") == ["n/a", "n/a", "-"], (
        "2025-09-15: pre and event windows end before 2025-10-01, post does not"
    )
    assert _cells(report, "test-event-2", "s2_l2a") == ["-", "listed(1)", "-"]
    for column in ("s1_slc", "s1_grd", "hyp3_insar", "s2_l2a", "nisar"):
        assert _cells(report, "test-event-3", column) == ["n/a"] * 3, column
    for column in ("dem_glo30", "era5", "gacos", "fdsn_waveforms", "usgs_comcat", "hydrometric"):
        assert _cells(report, "test-event-3", column) == ["-"] * 3, column


def test_footer_counts(report: CoverageReport) -> None:
    assert report.unresolved_refs == 1, "test-event-2's source sha256 has no ledger row"
    assert report.best_without_qualifying_source == 0
    assert report.ok is False
    assert report.footer() == "unresolved refs: 1; best without qualifying source: 0"


def test_renderers_mention_every_event(report: CoverageReport) -> None:
    table = render_table(report)
    markdown = render_markdown(report)
    payload = json.loads(render_json(report))
    for event_id in ("test-event-1", "test-event-2", "test-event-3"):
        assert event_id in table
        assert event_id in markdown
    assert table.splitlines()[0].startswith("event_id")
    assert "fetched(2)" in table
    assert table.rstrip().endswith(report.footer())
    assert markdown.splitlines()[0] == "| event_id | window | " + " | ".join(COLUMNS) + " |"
    assert markdown.splitlines()[1].startswith("|---|")
    assert report.footer() in markdown
    assert payload["unresolved_refs"] == 1
    assert payload["ok"] is False
    assert [r["event_id"] for r in payload["rows"]] == [r.event_id for r in report.rows]
    cell = payload["rows"][0]["cells"]["fdsn_waveforms"]["event"]
    assert cell == {"status": "fetched", "count": 2, "not_applicable": False, "text": "fetched(2)"}


def test_all_resolved_report_is_ok(tmp_path: Path, fictional: Fictional) -> None:
    events_dir = tmp_path / "events"
    fictional.write(events_dir, fictional.event())
    ledger = JsonlManifestLedger(tmp_path / "manifest.jsonl")
    ledger.append(fictional.ledger_row())
    report = build_report(events_dir, ledger)
    assert report.ok
    assert report.unresolved_refs == 0


def test_windows_and_overlap() -> None:
    pre, event, post = windows_for(T)
    assert (pre.start, pre.end, pre.end_inclusive) == (T - 90 * DAY, T, False)
    assert (event.start, event.end) == (T - DAY, T + DAY)
    assert (post.start, post.end, post.start_inclusive) == (T, T + 90 * DAY, False)
    assert not pre.overlaps(T, T + DAY), "pre excludes t itself"
    assert pre.overlaps(T - DAY, T - DAY)
    assert not post.overlaps(T - DAY, T), "post excludes t itself"
    assert post.overlaps(T, T + timedelta(seconds=1))
    assert event.overlaps(T - 5 * DAY, T - DAY), "touching the closed start counts"
    w = Window("x", T, T + DAY)
    assert w.entirely_before(T + 2 * DAY)
    assert not w.entirely_before(T + DAY), "closed end touching the instant is not before it"
    assert Window("y", T, T + DAY, end_inclusive=False).entirely_before(T + DAY)


def test_column_matches_sentinel1_levels(fictional: Fictional) -> None:
    row = fictional.ledger_row
    slc = row(DataSource.sentinel1_asf, product_level="SLC")
    grd = row(DataSource.sentinel1_asf, product_level="GRD_HD")
    none = row(DataSource.sentinel1_asf, product_level=None)
    other = row(DataSource.sentinel1_asf, product_level="OCN")
    assert column_matches("s1_slc", slc) and not column_matches("s1_grd", slc)
    assert column_matches("s1_grd", grd) and not column_matches("s1_slc", grd)
    assert column_matches("s1_slc", none) and column_matches("s1_grd", none)
    assert not column_matches("s1_slc", other) and not column_matches("s1_grd", other)
    assert not column_matches("hyp3_insar", slc)
    assert column_matches("fdsn_waveforms", row(DataSource.seedlink))
    assert column_matches("s2_l2a", row(DataSource.sentinel2_earthsearch))
