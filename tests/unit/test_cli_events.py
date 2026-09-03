"""`serac events` through typer's CliRunner on fictional tmp repositories. Offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd
from typer.testing import CliRunner

from serac.cli_events import app
from serac.domain.events import MassMovementEvent
from serac.pipelines.event_entry import interactive_record
from serac.pipelines.sources import dump_record

if TYPE_CHECKING:
    from tests.unit.conftest import Fictional

runner = CliRunner()

SHA = "a" * 64
NOTE = "fictional interactive record: figure not researched"


def _unknown(path_note: str = NOTE) -> list[str]:
    """Answers for a Range prompt that is unknown: 'unknown', reason, notes."""
    return ["unknown", "not_yet_researched", path_note]


def interactive_answers() -> list[str]:
    """The scripted answers for a minimal fictional record, in prompt order."""
    answers: list[str] = [
        "test-event-9",  # event_id
        "Fictional interactive event",  # name
        "test-event-9",  # event_group
        "reference",  # role
        "",  # aoi_id -> null
        "bedrock_rock_ice_avalanche",  # failure_type
        "2026-01-01T00:00:00Z",  # time.datetime_utc
        "",  # time.uncertainty_s -> null
        "test",  # time.basis
        "test-src-1",  # time.source_refs
        "1.0",  # lat
        "2.0",  # lon
        "",  # uncertainty_radius_m -> null
        "test",  # source_location.basis
        "test-src-1",  # source_location.source_refs
    ]
    answers += _unknown()  # source_elevation_m
    answers += ["1,2,1.5", "m", "", "test-src-1"]  # fall_height_m; empty source ids re-prompted
    for _ in ("source_volume_m3", "rock_fraction", "bulked_volume_m3"):
        answers += _unknown()
    for _ in ("runout_km", "peak_velocity_ms", "fatalities"):
        answers += _unknown()
    answers += ["n", "y", "", ""]  # dammed_river, secondary_surge, initially_reported_as, notes
    answers += ["y", "testid1"]  # seismic attribution? usgs_id
    answers += _unknown()  # seismic.magnitude
    answers += [""]  # mag_type
    answers += _unknown()  # seismic.agency_range
    answers += ["test-src-1", "n", ""]  # seismic.source_refs, single_force, notes
    answers += [
        "y",  # add a source?
        "peer_reviewed",  # kind
        "test-src-1",  # id
        "Fictional test source",  # title
        "https://example.invalid/test-src-1",  # url
        "",  # doi
        "Doe,Roe",  # authors (comma-separated)
        "2020",  # year
        "",  # publisher
        "2026-01-01T00:00:00Z",  # accessed_utc
        SHA,  # sha256
        "text/html",  # content_type
        "CC-BY-4.0",  # licence
        "",  # stored_copy
        "fall_height_m,time,source_location,seismic",  # claims_supported
        "",  # excerpt
        "n",  # add another source?
        "test",  # record.created_by
    ]
    return answers


class ScriptedAsk:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        return self.answers.pop(0)


# --- add ------------------------------------------------------------------------------------------


def test_add_from_json_writes_canonical_record(tmp_path: Path, fictional: Fictional) -> None:
    source = tmp_path / "in.json"
    record = fictional.event("test-event-1")
    source.write_text(json.dumps(record), encoding="utf-8")
    result = runner.invoke(app, ["add", "--from-json", str(source), "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = tmp_path / "data" / "events" / "test-event-1.json"
    assert result.stdout.strip() == str(out)
    text = out.read_text(encoding="utf-8")
    assert text == dump_record(json.loads(text)), "canonical: sorted keys, 2-space indent"
    assert MassMovementEvent.model_validate_json(text).event_id == "test-event-1"

    again = runner.invoke(app, ["add", "--from-json", str(source), "--repo", str(tmp_path)])
    assert again.exit_code == 1
    assert "--force" in again.output
    forced = runner.invoke(
        app, ["add", "--from-json", str(source), "--repo", str(tmp_path), "--force"]
    )
    assert forced.exit_code == 0, forced.output


def test_add_invalid_record_writes_nothing(tmp_path: Path, fictional: Fictional) -> None:
    source = tmp_path / "in.json"
    record = fictional.event("test-event-1")
    del record["sources"]
    source.write_text(json.dumps(record), encoding="utf-8")
    events_dir = tmp_path / "events"
    result = runner.invoke(
        app, ["add", "--from-json", str(source), "--events-dir", str(events_dir)]
    )
    assert result.exit_code == 1
    assert "sources" in result.output
    assert not events_dir.exists() or not list(events_dir.iterdir())

    source.write_text("{not json", encoding="utf-8")
    result = runner.invoke(
        app, ["add", "--from-json", str(source), "--events-dir", str(events_dir)]
    )
    assert result.exit_code == 1
    assert not events_dir.exists() or not list(events_dir.iterdir())


def test_interactive_record_via_injectable_prompt(fictional: Fictional) -> None:
    ask = ScriptedAsk(interactive_answers())
    data = interactive_record(ask, now=fictional.time)
    assert ask.answers == [], "every scripted answer was consumed"
    event = MassMovementEvent.model_validate(data)
    assert event.event_id == "test-event-9"
    assert event.aoi_id is None
    assert event.fall_height_m is not None and event.fall_height_m.best == 1.5
    assert event.fall_height_m.source_refs == ["test-src-1"]
    assert event.source_elevation_m is None
    assert event.field_notes["source_elevation_m"].notes == NOTE
    assert "seismic.magnitude" in event.field_notes
    assert "seismic.agency_range" in event.field_notes
    assert event.seismic is not None and event.seismic.usgs_id == "testid1"
    assert event.secondary_surge is True and event.dammed_river is False
    assert event.sources[0].peer_reviewed is True
    assert event.sources[0].authors == ["Doe", "Roe"]
    assert event.sources[0].year == 2020
    assert event.record.created_utc == fictional.time
    assert sum(q.startswith("fall_height_m: source ids") for q in ask.questions) == 2, (
        "an empty source-id list is re-prompted"
    )
    assert any(
        q.startswith("source_elevation_m (low,high[,best] or 'unknown')") for q in ask.questions
    )


def test_interactive_add_via_cli_runner(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    result = runner.invoke(
        app,
        ["add", "--events-dir", str(events_dir)],
        input="\n".join(interactive_answers()) + "\n",
    )
    assert result.exit_code == 0, result.output
    out = events_dir / "test-event-9.json"
    assert out.exists()
    event = MassMovementEvent.model_validate_json(out.read_text(encoding="utf-8"))
    assert event.name == "Fictional interactive event"
    assert event.infrastructure_impacts == []


def test_interactive_add_rejects_invalid_record(tmp_path: Path) -> None:
    answers = interactive_answers()
    answers[answers.index("test-src-1")] = "test-src-missing"  # time.source_refs dangles
    events_dir = tmp_path / "events"
    result = runner.invoke(
        app, ["add", "--events-dir", str(events_dir)], input="\n".join(answers) + "\n"
    )
    assert result.exit_code == 1
    assert "test-src-missing" in result.output
    assert not events_dir.exists()


# --- build-index, report, validate ----------------------------------------------------------------


def test_build_index_command(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path, index=False)
    result = runner.invoke(app, ["build-index", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    path = Path(result.stdout.strip())
    assert path == repo / "data" / "events" / "events.parquet"
    assert len(gpd.read_parquet(path)) == 4


def test_report_formats(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path, [fictional.event("test-event-1")])
    for fmt in ("table", "markdown"):
        result = runner.invoke(app, ["report", "--format", fmt, "--repo", str(repo)])
        assert result.exit_code == 0, result.output
        assert "test-event-1" in result.stdout
        assert "unresolved refs: 0; best without qualifying source: 0" in result.output
    result = runner.invoke(app, ["report", "--format", "json", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["rows"][0]["event_id"] == "test-event-1"

    out = tmp_path / "reports" / "coverage.md"
    result = runner.invoke(
        app, ["report", "--format", "markdown", "--repo", str(repo), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "test-event-1" in out.read_text(encoding="utf-8")


def test_report_exits_1_on_unresolved_reference(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path, [fictional.event("test-event-1", sha256="c" * 64)])
    result = runner.invoke(app, ["report", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "unresolved refs: 1" in result.output


def test_validate_passes_on_complete_repo(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path)
    result = runner.invoke(app, ["validate", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "events: passed" in result.output
    report = json.loads((repo / "reports" / "validation" / "events.json").read_text())
    assert report["suite"] == "events"
    assert all(c["ok"] for c in report["checks"])


def test_validate_fails_on_stale_index(tmp_path: Path, fictional: Fictional) -> None:
    repo = fictional.repo(tmp_path)
    path = repo / "data" / "events" / "test-target.json"
    record = fictional.read(path)
    record["name"] = "Fictional target, edited after indexing"
    path.write_text(dump_record(record), encoding="utf-8")
    result = runner.invoke(app, ["validate", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "FAIL index: up to date" in result.output
