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
from serac.streaming.cap_stage import build_cap_stage, cap_lane_banner, parse_cap_kind
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
    """Real-time seismic lane (Test-only CAP; not an alert system)."""


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
    detector: Annotated[
        str,
        typer.Option(
            "--detector",
            help=(
                "stub (default) or discriminator. The stub stays the default while "
                "validate-discriminator reports an unmet criterion; the same rule the replay "
                "lane follows."
            ),
        ),
    ] = "stub",
    repo: RepoOption = Path("."),
    inventory: Annotated[
        Path | None,
        typer.Option(
            "--inventory",
            help="StationXML for instrument response. Required by the trained detector.",
        ),
    ] = None,
) -> None:
    """Run the detector stage (serac.waveforms -> serac.detections).

    Until 2026-09-10 this command could only run the stub, so the live lane and the replay lane
    disagreed about what serac could do: `serac replay --detector discriminator` mounted the
    trained model and `serac stream run detector` had no way to. Both now call the same factory.

    Selecting `discriminator` mounts a real model and **does not** make this an alert system: the
    CAP stage downstream still emits `status=Test` (research output, not an operational alert),
    and the model itself has never been promoted.
    """
    if detector not in {"stub", "discriminator"}:
        typer.echo(
            f"serac stream run detector: unknown detector {detector!r}; use stub or discriminator",
            err=True,
        )
        raise typer.Exit(1)
    if detector == "stub":
        _run_stage(
            DetectorStub(DetectorStubConfig(allow_synthetic=allow_synthetic)),
            _bus(bus),
            max_seconds,
        )
        return

    from serac.pipelines.replay import ReplayError, build_trained_detector
    from serac.streaming.detector_stage import DetectorStage

    try:
        trained = build_trained_detector("discriminator", repo_root=repo, inventory_path=inventory)
    except ReplayError as exc:
        # A missing artifact is an error, never a quiet fall back to the stub: a lane that says
        # it ran the trained model must have run it.
        typer.echo(f"serac stream run detector: {exc}", err=True)
        raise typer.Exit(1) from exc
    info = trained.info()
    typer.echo(
        f"detector={info.name} version={info.version} is_stub={info.is_stub}; the CAP stage "
        "downstream emits status=Test (research output, not an alert system)"
    )
    _run_stage(DetectorStage(trained), _bus(bus), max_seconds)


@run_app.command("cap")
def run_cap(
    bus: BusOption = "in_memory",
    max_seconds: Annotated[float | None, typer.Option("--max-seconds")] = None,
    repo: RepoOption = Path("."),
    cap: Annotated[
        str,
        typer.Option(
            "--cap",
            help=(
                "xsd (default): real CAP 1.2 generator via serac.adapters.cap.cap12.render. "
                "stub: CapStub, for golden fixtures that pin stub XML."
            ),
        ),
    ] = "xsd",
) -> None:
    """Render detections to CAP 1.2 (status=Test). This is not an alert system."""
    try:
        kind = parse_cap_kind(cap)
    except ValueError as exc:
        typer.echo(f"serac stream run cap: {exc}", err=True)
        raise typer.Exit(1) from exc
    xsd = repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"
    typer.echo(cap_lane_banner(kind))
    _run_stage(build_cap_stage(kind, xsd_path=xsd), _bus(bus), max_seconds)


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
    cap: Annotated[
        str,
        typer.Option(
            "--cap",
            help=(
                "xsd (default): real CAP 1.2 generator. stub: CapStub, for fixtures that "
                "pin stub XML."
            ),
        ),
    ] = "xsd",
    bus: BusOption = "in_memory",
    report_dir: Annotated[Path | None, typer.Option("--report-dir")] = None,
    online: Annotated[
        bool, typer.Option("--online", help="Fetch from FDSN if no fixture.")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Replay an event window through the lane and write reports/replay/<event>.json.

    CAP messages are status=Test. This is not an alert system.
    """
    if bus not in ("in_memory", "redis"):
        raise typer.BadParameter(f"unknown bus {bus!r}; use in_memory or redis")
    try:
        cap_kind = parse_cap_kind(cap)
    except ValueError as exc:
        typer.echo(f"serac replay: {exc}", err=True)
        raise typer.Exit(1) from exc
    config = ReplayConfig(
        event_id=event,
        speed=parse_speed(speed),
        chunk_seconds=chunk_seconds,
        bus="redis" if bus == "redis" else "in_memory",
        detector_kind="discriminator" if detector == "discriminator" else "stub",
        cap_kind=cap_kind,
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
    typer.echo(cap_lane_banner(cap_kind))
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
