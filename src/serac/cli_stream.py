"""`serac stream ...` and `serac replay ...`: the real-time lane from the command line.

Exposes:

* `app` — Typer with `run seedlink|detector|cap [--bus in_memory|redis]`, `golden`, and
  `replay`; mount as `serac stream`.
* `replay` — the command function, so `serac.cli` can also mount it at the top level as
  `serac replay` (`app.command("replay")(cli_stream.replay)`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Literal

import typer

from serac.adapters.bus.in_memory import InMemoryBus
from serac.pipelines.replay import ReplayConfig, parse_speed, run_replay
from serac.ports.bus import MessageBus
from serac.settings import get_settings
from serac.streaming.cap_stub import CapStub
from serac.streaming.detector_stub import DetectorStub, DetectorStubConfig
from serac.streaming.golden import (
    DEFAULT_EVENT,
    compute_golden,
    diff_golden,
    golden_path,
    load_golden,
    write_golden,
)
from serac.streaming.replay_source import FixtureNotFetchedError
from serac.streaming.stage import Stage, StageRunner

BusName = Literal["in_memory", "redis"]

app = typer.Typer(
    name="stream", help="Real-time lane: run stages, replay, golden.", no_args_is_help=True
)
run_app = typer.Typer(name="run", help="Run one stage against a bus.", no_args_is_help=True)
app.add_typer(run_app, name="run")

BusOption = Annotated[
    str, typer.Option("--bus", help="in_memory (single process) or redis (SERAC_REDIS_URL).")
]
RepoOption = Annotated[Path, typer.Option("--repo", help="Repository root.")]


@app.callback()
def _group() -> None:
    """Real-time seismic lane (stub detector, Test-only CAP)."""


def _bus(name: str) -> MessageBus:
    if name == "in_memory":
        return InMemoryBus()
    if name == "redis":
        from serac.adapters.bus.redis_streams import RedisStreamsBus

        return RedisStreamsBus.from_url(get_settings().serac_redis_url)
    raise typer.BadParameter(f"unknown bus {name!r}; use in_memory or redis")


def _run_stage(stage: Stage, bus: MessageBus, max_seconds: float | None) -> None:
    runner = StageRunner(bus, stage)
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    try:
        processed = runner.run_forever(
            should_stop=lambda: deadline is not None and time.monotonic() >= deadline
        )
    except KeyboardInterrupt:
        processed = runner.processed
    finally:
        bus.close()
    typer.echo(f"{stage.name}: processed {processed}, published {runner.published}")


@run_app.command("seedlink")
def run_seedlink(
    stream: Annotated[
        list[str], typer.Option("--stream", help="NET.STA.LOC.CHA to subscribe (repeatable).")
    ],
    bus: BusOption = "in_memory",
    server: Annotated[
        str | None, typer.Option("--server", help="host:port; default from settings.")
    ] = None,
    max_chunks: Annotated[
        int | None, typer.Option("--max-chunks", help="Stop after N chunks.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Describe; do not connect.")] = False,
) -> None:
    """Ingest a SeedLink feed onto serac.waveforms (endpoint unverified; see RELEASE_STATUS)."""
    from serac.adapters.seismic.seedlink import SeedLinkFeed
    from serac.domain.seismic import Sncl
    from serac.streaming.seedlink_ingestor import SeedLinkIngestor

    sncls = [Sncl.from_key(key) for key in stream]
    feed = SeedLinkFeed(server)
    feed.subscribe(sncls)
    if dry_run:
        for key, value in feed.describe().as_dict().items():
            typer.echo(f"{key}: {value}")
        return
    message_bus = _bus(bus)
    try:
        summary = SeedLinkIngestor(feed, message_bus).run(sncls, max_chunks=max_chunks)
    finally:
        message_bus.close()
    typer.echo(f"published {summary.chunks_published} chunks from {feed.server}")


@run_app.command("detector")
def run_detector(
    bus: BusOption = "in_memory",
    max_seconds: Annotated[float | None, typer.Option("--max-seconds")] = None,
    allow_synthetic: Annotated[bool, typer.Option("--allow-synthetic")] = False,
) -> None:
    """Run the STUB detector (serac.waveforms -> serac.detections)."""
    _run_stage(
        DetectorStub(DetectorStubConfig(allow_synthetic=allow_synthetic)), _bus(bus), max_seconds
    )


@run_app.command("cap")
def run_cap(
    bus: BusOption = "in_memory",
    max_seconds: Annotated[float | None, typer.Option("--max-seconds")] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Run the CAP stub (serac.detections -> serac.alerts, status=Test)."""
    xsd = repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"
    _run_stage(CapStub(xsd_path=xsd), _bus(bus), max_seconds)


@app.command("golden")
def golden(
    event: Annotated[str, typer.Option("--event")] = DEFAULT_EVENT,
    update: Annotated[bool, typer.Option("--update", help="Rewrite the golden file.")] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Check (or --update) the detector stub's golden ratio record for a real fixture."""
    path = golden_path(repo, event)
    actual = compute_golden(repo, event)
    if update:
        write_golden(actual, path)
        typer.echo(f"wrote {path} ({actual['n_ratios']} ratios, {actual['n_fired']} fired)")
        return
    if not path.exists():
        typer.echo(f"{path} missing; run with --update", err=True)
        raise typer.Exit(code=1)
    diff = diff_golden(load_golden(path), actual)
    if diff:
        for line in diff:
            typer.echo(line, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"golden matches {path}")


@app.command("replay")
def replay(
    event: Annotated[str, typer.Option("--event", help="Fixture event id or synthetic-lp-burst.")],
    speed: Annotated[str, typer.Option("--speed", help="1.0 (paced) or max.")] = "max",
    chunk_seconds: Annotated[float, typer.Option("--chunk-seconds")] = 5.0,
    detector: Annotated[
        str,
        typer.Option(
            "--detector",
            help=(
                "stub (default) or discriminator. The stub remains the default while "
                "validate-discriminator reports an unmet criterion."
            ),
        ),
    ] = "stub",
    bus: BusOption = "in_memory",
    report_dir: Annotated[Path | None, typer.Option("--report-dir")] = None,
    online: Annotated[
        bool, typer.Option("--online", help="Fetch from FDSN if no fixture.")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Replay an event window through the lane and write reports/replay/<event>.json."""
    if bus not in ("in_memory", "redis"):
        raise typer.BadParameter(f"unknown bus {bus!r}; use in_memory or redis")
    config = ReplayConfig(
        event_id=event,
        speed=parse_speed(speed),
        chunk_seconds=chunk_seconds,
        bus="redis" if bus == "redis" else "in_memory",
        detector_kind="discriminator" if detector == "discriminator" else "stub",
        report_dir=report_dir,
        online=online,
        repo_root=repo,
    )
    try:
        report = run_replay(config, fetch_online=_fetch_online if online else None)
    except FixtureNotFetchedError as exc:
        typer.echo(f"not fetched: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    out = (report_dir or repo / "reports" / "replay") / f"{event}.json"
    c = report.counts
    typer.echo(
        f"{event}: {report.status}; chunks {c.chunks_published}/{c.chunks_consumed} "
        f"(published/consumed), pending {c.pending_after_drain}, detections "
        f"{c.detections_emitted}, cap {c.cap_messages_emitted}; "
        f"detector={report.detector.name} stub={report.detector.is_stub} -> {out}"
    )
    if report.status != "completed":
        typer.echo(f"error: {report.error}", err=True)
        raise typer.Exit(code=1)


def _fetch_online(config: ReplayConfig) -> Path:
    """FDSN fetch for a replay with no fixture: needs an event record for time and place."""
    from datetime import timedelta

    from serac.adapters.seismic.fdsn import FdsnWaveformArchive
    from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
    from serac.pipelines.replay import ReplayError, load_origin
    from serac.ports.seismic import StationQuery, WaveformRequest

    origin = load_origin(config.repo_root, config.event_id)
    if origin.origin_time_utc is None or origin.latitude is None or origin.longitude is None:
        raise ReplayError(
            f"--online needs data/events/{config.event_id}.json with time and source_location"
        )
    archive = FdsnWaveformArchive(repo_root=config.repo_root)
    start = origin.origin_time_utc - timedelta(minutes=2)
    end = origin.origin_time_utc + timedelta(minutes=6)
    stations = archive.search_stations(
        StationQuery(
            latitude=origin.latitude,
            longitude=origin.longitude,
            max_radius_km=500,
            start_utc=start,
            end_utc=end,
        )
    )
    if not stations:
        raise ReplayError("no open broadband channels within 500 km for the window")
    request = WaveformRequest(
        event_id=config.event_id, sncls=[s.sncl for s in stations[:4]], start_utc=start, end_utc=end
    )
    plan = archive.plan(request)
    typer.echo(
        f"fetching ~{plan.estimated_bytes} B from {plan.data_centre} ({plan.estimate_basis})"
    )
    dest = config.repo_root / "data" / "raw" / "fdsn_waveforms" / config.event_id
    ledger = JsonlManifestLedger(config.repo_root / "data" / "manifest.jsonl")
    archive.fetch(plan, dest, ledger)
    return dest


if __name__ == "__main__":  # pragma: no cover
    app()
