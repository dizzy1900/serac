"""The coverage matrix behind `serac events report`.

Rows are event records, columns are the data products serac ingests, and each cell is the
best provenance-ledger status (with a count) among the ledger rows for that product that
overlap one of three windows around the event time:

    pre   = [t - 90 d, t)      event = [t - 1 d, t + 1 d]      post = (t, t + 90 d]

A cell is `n/a` when the product did not exist yet for the whole window (dated availability
constants below), `-` when nothing is ledgered, and e.g. `fetched(2)` otherwise. The status
rank is `fetched > requested > listed > not_fetched > failed > dry_run > synthetic`: the cell
reports the most-complete status, and the count says how many rows sit behind it in total.

The footer counts two defects the library must not carry: source references that do not
resolve (a `source_refs` id with no `sources[]` entry, or a `sources[].sha256` that no ledger
row carries) and `best` values without a qualifying source. `serac events report` exits
non-zero unless both are 0.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from serac.domain.common import BEST_QUALIFYING_KINDS, iter_ranges, iter_source_ref_ids
from serac.domain.events import MassMovementEvent
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus
from serac.pipelines.events_index import load_records
from serac.ports.ledger import ManifestLedger

COLUMNS: tuple[str, ...] = (
    "s1_slc",
    "s1_grd",
    "hyp3_insar",
    "s2_l2a",
    "nisar",
    "dem_glo30",
    "era5",
    "gacos",
    "fdsn_waveforms",
    "usgs_comcat",
    "hydrometric",
)

WINDOWS: tuple[str, ...] = ("pre", "event", "post")

PRE_DAYS = 90
POST_DAYS = 90
EVENT_HALF_WIDTH = timedelta(days=1)

COLUMN_SOURCES: dict[str, tuple[DataSource, ...]] = {
    "s1_slc": (DataSource.sentinel1_asf,),
    "s1_grd": (DataSource.sentinel1_asf,),
    "hyp3_insar": (DataSource.hyp3_insar,),
    "s2_l2a": (DataSource.sentinel2_cdse, DataSource.sentinel2_earthsearch),
    "nisar": (DataSource.nisar_asf,),
    "dem_glo30": (DataSource.dem_glo30,),
    "era5": (DataSource.era5_cds,),
    "gacos": (DataSource.gacos,),
    "fdsn_waveforms": (DataSource.fdsn_waveforms, DataSource.seedlink),
    "usgs_comcat": (DataSource.usgs_comcat,),
    "hydrometric": (DataSource.hydrometric_icimod,),
}

# Dated availability: a column is `n/a` for a window that ends before the product existed.
# Sentinel-1A first acquisitions entered the open archive on 2014-10-03 (the same date bounds
# HyP3 InSAR, which is built from Sentinel-1 SLCs); Sentinel-2A launched 2015-06-23; NISAR
# science data are treated as unavailable before 2025-10-01. `dem_glo30`, `era5`, `gacos`,
# `fdsn_waveforms`, `usgs_comcat` and `hydrometric` are never `n/a` here.
AVAILABLE_FROM: dict[str, datetime] = {
    "s1_slc": datetime(2014, 10, 3, tzinfo=UTC),
    "s1_grd": datetime(2014, 10, 3, tzinfo=UTC),
    "hyp3_insar": datetime(2014, 10, 3, tzinfo=UTC),
    "s2_l2a": datetime(2015, 6, 23, tzinfo=UTC),
    "nisar": datetime(2025, 10, 1, tzinfo=UTC),
}

STATUS_RANK: dict[ManifestStatus, int] = {
    ManifestStatus.fetched: 0,
    ManifestStatus.requested: 1,
    ManifestStatus.listed: 2,
    ManifestStatus.not_fetched: 3,
    ManifestStatus.failed: 4,
    ManifestStatus.dry_run: 5,
    ManifestStatus.synthetic: 6,
}

NOT_APPLICABLE = "n/a"
EMPTY = "-"


@dataclass(frozen=True)
class Window:
    """A time interval with explicit end-point inclusivity."""

    name: str
    start: datetime
    end: datetime
    start_inclusive: bool = True
    end_inclusive: bool = True

    def overlaps(self, time_start: datetime, time_end: datetime) -> bool:
        """True when the closed span `[time_start, time_end]` intersects the window."""
        if time_end < self.start or (time_end == self.start and not self.start_inclusive):
            return False
        return not (time_start > self.end or (time_start == self.end and not self.end_inclusive))

    def entirely_before(self, instant: datetime) -> bool:
        return self.end < instant or (self.end == instant and not self.end_inclusive)


def windows_for(t: datetime) -> tuple[Window, ...]:
    """The `pre`, `event` and `post` windows around an event time, in `WINDOWS` order."""
    return (
        Window("pre", t - timedelta(days=PRE_DAYS), t, end_inclusive=False),
        Window("event", t - EVENT_HALF_WIDTH, t + EVENT_HALF_WIDTH),
        Window("post", t, t + timedelta(days=POST_DAYS), start_inclusive=False),
    )


class Cell(BaseModel):
    """One matrix cell: the best status among the matching ledger rows, and how many."""

    model_config = ConfigDict(frozen=True)

    status: ManifestStatus | None = None
    count: int = 0
    not_applicable: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def text(self) -> str:
        if self.not_applicable:
            return NOT_APPLICABLE
        if self.status is None or self.count == 0:
            return EMPTY
        return f"{self.status.value}({self.count})"


class CoverageRow(BaseModel):
    """One record's cells, keyed `cells[column][window]`."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    name: str
    role: str
    time_utc: datetime
    cells: dict[str, dict[str, Cell]]

    def cell(self, column: str, window: str) -> Cell:
        return self.cells[column][window]


class CoverageReport(BaseModel):
    """The matrix plus the footer counts that gate `serac events report`."""

    model_config = ConfigDict(frozen=True)

    generated_utc: datetime
    columns: tuple[str, ...] = COLUMNS
    windows: tuple[str, ...] = WINDOWS
    rows: list[CoverageRow]
    unresolved_refs: int
    best_without_qualifying_source: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.unresolved_refs == 0 and self.best_without_qualifying_source == 0

    def footer(self) -> str:
        return (
            f"unresolved refs: {self.unresolved_refs}; "
            f"best without qualifying source: {self.best_without_qualifying_source}"
        )


# --- matrix ------------------------------------------------------------------------------------


def column_matches(column: str, entry: ManifestEntry) -> bool:
    """Whether a ledger row counts under `column` (Sentinel-1 splits on `product_level`)."""
    if entry.source not in COLUMN_SOURCES[column]:
        return False
    if column in ("s1_slc", "s1_grd"):
        level = entry.product_level
        if level is None:
            return True
        return ("SLC" if column == "s1_slc" else "GRD") in level.upper()
    return True


def _entry_span(entry: ManifestEntry) -> tuple[datetime, datetime] | None:
    if entry.time_start is None and entry.time_end is None:
        return None
    start = entry.time_start if entry.time_start is not None else entry.time_end
    end = entry.time_end if entry.time_end is not None else entry.time_start
    assert start is not None and end is not None
    return start, end


def entries_for_record(
    record: MassMovementEvent, entries: Iterable[ManifestEntry]
) -> list[tuple[ManifestEntry, bool]]:
    """Ledger rows matched to a record by `event_id` or (when set) `aoi_id`, deduplicated.

    The flag says whether the row matched by `event_id`: an undated row counts in every window
    only when it was ledgered for this specific event.
    """
    seen: dict[str, tuple[ManifestEntry, bool]] = {}
    for entry in entries:
        by_event = entry.event_id == record.event_id
        by_aoi = record.aoi_id is not None and entry.aoi_id == record.aoi_id
        if not (by_event or by_aoi):
            continue
        previous = seen.get(entry.entry_id)
        seen[entry.entry_id] = (entry, by_event or (previous is not None and previous[1]))
    return list(seen.values())


def build_cell(column: str, window: Window, matched: Iterable[tuple[ManifestEntry, bool]]) -> Cell:
    """The cell for one (record, column, window)."""
    available_from = AVAILABLE_FROM.get(column)
    if available_from is not None and window.entirely_before(available_from):
        return Cell(not_applicable=True)
    best: ManifestStatus | None = None
    count = 0
    for entry, by_event in matched:
        if not column_matches(column, entry):
            continue
        span = _entry_span(entry)
        if span is None:
            if not by_event:
                continue
        elif not window.overlaps(*span):
            continue
        count += 1
        if best is None or STATUS_RANK[entry.status] < STATUS_RANK[best]:
            best = entry.status
    return Cell(status=best, count=count)


def build_row(record: MassMovementEvent, entries: Iterable[ManifestEntry]) -> CoverageRow:
    matched = entries_for_record(record, entries)
    windows = windows_for(record.time.datetime_utc)
    cells = {
        column: {window.name: build_cell(column, window, matched) for window in windows}
        for column in COLUMNS
    }
    return CoverageRow(
        event_id=record.event_id,
        name=record.name,
        role=record.role.value,
        time_utc=record.time.datetime_utc,
        cells=cells,
    )


# --- footer ------------------------------------------------------------------------------------


def unresolved_reference_count(record: MassMovementEvent, ledger_sha256s: set[str]) -> int:
    """`source_refs` ids with no `sources[]` entry plus source sha256s with no ledger row."""
    known = {s.id for s in record.sources}
    dangling = sum(1 for _path, ref in iter_source_ref_ids(record) if ref not in known)
    unledgered = sum(1 for s in record.sources if s.sha256 not in ledger_sha256s)
    return dangling + unledgered


def best_without_qualifying_source_count(record: MassMovementEvent) -> int:
    """Ranges carrying `best` whose `source_refs` include no `BEST_QUALIFYING_KINDS` source."""
    by_id = {s.id: s for s in record.sources}
    total = 0
    for _path, rng in iter_ranges(record):
        if rng.best is None:
            continue
        kinds = {by_id[r].kind for r in rng.source_refs if r in by_id}
        if not kinds & BEST_QUALIFYING_KINDS:
            total += 1
    return total


def build_report(
    events_dir: Path, ledger: ManifestLedger, now: datetime | None = None
) -> CoverageReport:
    """Read every record and the whole ledger once, then assemble the matrix and footer."""
    records = [event for _path, event in load_records(events_dir)]
    entries = list(ledger.entries())
    ledger_sha256s = {e.sha256 for e in entries if e.sha256 is not None}
    rows = [build_row(record, entries) for record in sorted(records, key=lambda r: r.event_id)]
    return CoverageReport(
        generated_utc=now or datetime.now(tz=UTC),
        rows=rows,
        unresolved_refs=sum(unresolved_reference_count(r, ledger_sha256s) for r in records),
        best_without_qualifying_source=sum(
            best_without_qualifying_source_count(r) for r in records
        ),
    )


# --- renderers ---------------------------------------------------------------------------------

_HEADER = ("event_id", "window")


def _grid(report: CoverageReport) -> list[list[str]]:
    """Header row followed by one line per (record, window)."""
    lines = [[*_HEADER, *report.columns]]
    for row in report.rows:
        for window in report.windows:
            lines.append(
                [
                    row.event_id,
                    window,
                    *(row.cell(column, window).text for column in report.columns),
                ]
            )
    return lines


def render_table(report: CoverageReport) -> str:
    """Fixed-width plain text: one line per (record, window), footer last."""
    grid = _grid(report)
    widths = [max(len(line[i]) for line in grid) for i in range(len(grid[0]))]
    out: list[str] = []
    for n, line in enumerate(grid):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    out.append("")
    out.append(report.footer())
    return "\n".join(out) + "\n"


def render_markdown(report: CoverageReport) -> str:
    """A GitHub-flavoured Markdown table with the footer beneath it."""
    grid = _grid(report)
    out = ["| " + " | ".join(grid[0]) + " |", "|" + "|".join("---" for _ in grid[0]) + "|"]
    out.extend("| " + " | ".join(line) + " |" for line in grid[1:])
    out.append("")
    out.append(report.footer())
    return "\n".join(out) + "\n"


def render_json(report: CoverageReport) -> str:
    """The report as JSON (cells carry `status`, `count`, `not_applicable` and `text`)."""
    payload: dict[str, Any] = json.loads(report.model_dump_json())
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
