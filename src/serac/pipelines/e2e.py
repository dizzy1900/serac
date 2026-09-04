"""`serac cascade e2e`: waveform -> detection -> LFH -> surrogate -> CAP -> avoided loss.

The chain is run stage by stage and **stops at the first stage that cannot give its successor
what that successor needs**. Every later stage is then recorded as `not_reached`, quoting the
measured reason from the stage that blocked it. Nothing is substituted: no default mass, no
default location, no nominal footprint. A pipeline that filled those in would turn a refusal
into a forecast, which is the one outcome this lane exists to prevent.

Stage sources
-------------
`waveform` and `detection` are **executed here**, on the committed seismic fixtures, with the
real M1 discriminator loaded from `baselines/discriminator/`. `lfh` is executed when the
committed Green's-function fixtures cover the event and falls back to M2's own committed run
otherwise (a fresh clone has no `data/raw/`). `runout` reads M4's frozen-ensemble artifact.
Every `StageEvidence` says which, and carries the artifact's sha256.

What the reports contain
------------------------
`reports/e2e/<event>.md` and `.json`: the timeline, every stage's measured numbers, the
refusal text verbatim, and the avoided-loss response the chain produced (which, when the chain
stopped early, is an `INSUFFICIENT INPUT` response listing what was missing per asset).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from serac import __version__
from serac.cascade.compute import (
    CascadeLossResult,
    compute_avoided_loss,
)
from serac.cascade.damage import ReplacementValueRule
from serac.cascade.evidence import (
    Execution,
    StageEvidence,
    StageOutcome,
    discriminator_case_study,
    discriminator_latency,
    ensemble_arrivals,
    lfh_outcome,
    sha256_of,
    surrogate_latency_s,
)
from serac.cascade.exposure import ExposureBundle, load_exposure
from serac.cascade.prior import (
    PRIOR_MODEL_NAME,
    ensemble_prior_forecast,
    prior_request,
)
from serac.errors import SeracError
from serac.streaming.replay_source import FixtureReplaySource, fixture_dir_for

E2E_REPORT_VERSION = "0.1.0"
REPORT_SUBDIR = Path("reports") / "e2e"

CHAIN_STAGES: tuple[str, ...] = (
    "waveform",
    "detection",
    "lfh",
    "runout",
    "cap",
    "avoided_loss",
)


@dataclass(frozen=True)
class E2EEvent:
    """One replayable event and the AOI whose exposure it threatens."""

    event_id: str
    fixture_event_id: str
    aoi_id: str
    name: str
    has_frozen_ensemble: bool


EVENTS: dict[str, E2EEvent] = {
    "chamoli-2021": E2EEvent(
        event_id="chamoli-2021",
        fixture_event_id="chamoli-2021",
        aoi_id="chamoli-rishiganga",
        name="Chamoli / Rishiganga, 7 February 2021",
        has_frozen_ensemble=False,
    ),
    "langtang-lhende-2026": E2EEvent(
        event_id="langtang-lhende-2026",
        fixture_event_id="langtang-2026",
        aoi_id="lhende-khola-trishuli",
        name="Langtang Lirung / Lhende Khola / Trishuli, 26 August 2026",
        has_frozen_ensemble=True,
    ),
}


class E2EError(SeracError):
    """The end-to-end lane could not be started (bad event id, missing fixture)."""


@dataclass
class E2EResult:
    """Everything one replay produced, in chain order."""

    event: E2EEvent
    started_utc: datetime
    finished_utc: datetime
    serac_version: str
    stages: list[StageEvidence]
    stopped_at: str | None
    stopped_because: str | None
    loss: CascadeLossResult | None
    exposure: ExposureBundle | None
    cap_identifier: str | None = None
    cap_xml_path: str | None = None
    context: list[StageEvidence] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        """Whether the chain ran to its honest end -- which includes ending at a refusal."""
        return all(s.outcome != StageOutcome.unavailable for s in self.stages)

    def stage(self, name: str) -> StageEvidence | None:
        return next((s for s in self.stages if s.stage == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_version": E2E_REPORT_VERSION,
            "event_id": self.event.event_id,
            "event_name": self.event.name,
            "aoi_id": self.event.aoi_id,
            "serac_version": self.serac_version,
            "started_utc": self.started_utc.isoformat(),
            "finished_utc": self.finished_utc.isoformat(),
            "chain_completed": self.completed,
            "stopped_at": self.stopped_at,
            "stopped_because": self.stopped_because,
            "stages": [s.as_dict() for s in self.stages],
            "context": [s.as_dict() for s in self.context],
            "cap_identifier": self.cap_identifier,
            "cap_xml_path": self.cap_xml_path,
            "avoided_loss_response": (
                self.loss.response.model_dump(mode="json") if self.loss else None
            ),
            "avoided_loss_by_asset": (
                [a.model_dump(mode="json") for a in self.loss.by_asset] if self.loss else []
            ),
            "caveats": self.caveats,
        }


# -- stage 1: waveform --------------------------------------------------------------------------


def _waveform_stage(
    repo: Path, event: E2EEvent
) -> tuple[StageEvidence, FixtureReplaySource | None]:
    directory = fixture_dir_for(repo, event.fixture_event_id)
    manifest = directory / "manifest.json"
    if not manifest.exists():
        return (
            StageEvidence(
                stage="waveform",
                component="committed seismic fixture",
                outcome=StageOutcome.unavailable,
                execution=Execution.unavailable,
                summary=f"no fixture at {directory}",
                blocks_downstream=True,
            ),
            None,
        )
    source = FixtureReplaySource(directory, repo_root=repo)
    window = source.window()
    stations = source.stations()
    return (
        StageEvidence(
            stage="waveform",
            component="committed seismic fixture",
            outcome=StageOutcome.produced,
            execution=Execution.executed,
            summary=(
                f"{len(stations)} receiver(s) over "
                f"{window.start_utc.isoformat() if window else '?'} to "
                f"{window.end_utc.isoformat() if window else '?'}"
            ),
            artifact_path=str(manifest.relative_to(repo)),
            artifact_sha256=sha256_of(manifest),
            measured={
                "stations": [s.sncl for s in stations],
                "window_start_utc": window.start_utc.isoformat() if window else None,
                "window_end_utc": window.end_utc.isoformat() if window else None,
                "window_seconds": (
                    (window.end_utc - window.start_utc).total_seconds() if window else None
                ),
            },
            notes=source.caveats(),
        ),
        source,
    )


# -- stage 2: detection -------------------------------------------------------------------------


def _detection_stage(
    repo: Path, event: E2EEvent, source: FixtureReplaySource
) -> tuple[StageEvidence, StageEvidence]:
    """Execute the real M1 detector on the fixture, and read M1's own recorded latency run."""
    executed = _run_discriminator(repo, source)
    recorded = discriminator_latency(repo, event.event_id)
    return executed, recorded


def _run_discriminator(repo: Path, source: FixtureReplaySource) -> StageEvidence:
    from obspy import read_inventory

    from serac.models.discriminator import baseline as bl
    from serac.models.discriminator import streaming as st

    artifact_dir = repo / bl.ARTIFACT_DIR / "loro_hma"
    if not (artifact_dir / "artifact.json").exists():
        return StageEvidence(
            stage="detection",
            component="M1 discriminator (executed here)",
            outcome=StageOutcome.unavailable,
            execution=Execution.unavailable,
            summary=f"no trained artifact at {artifact_dir}",
            blocks_downstream=True,
        )
    stations_xml = source.fixture_dir / "stations.xml"
    inventory = (
        read_inventory(str(stations_xml), format="STATIONXML") if (stations_xml.exists()) else None
    )
    model = bl.load(artifact_dir)
    chunks = list(source.chunks(chunk_seconds=5.0))
    candidates: list[Any] = []
    modes: dict[str, Any] = {}
    all_modes: tuple[st.Mode, ...] = ("batch_600s", "sliding_180s")
    for mode in all_modes:
        detector = st.DiscriminatorDetector(
            model=model,
            inventory=inventory,
            require_response=inventory is not None,
            mode=mode,
        )
        started = time.perf_counter()
        fired: Any = None
        stream_time = None
        for chunk in sorted(chunks, key=lambda c: (c.start_time_utc, c.sncl.key)):
            detector.ingest(chunk)
            stream_time = (
                chunk.end_time_utc if stream_time is None else max(stream_time, chunk.end_time_utc)
            )
            found = detector.poll(stream_time)
            if found:
                fired = found[0]
                break
        modes[mode] = {
            "fired": fired is not None,
            "windows_scored": detector.windows_scored,
            "chunks_ingested": detector.chunks_seen,
            "compute_seconds_total": round(time.perf_counter() - started, 4),
            "probability": None if fired is None else fired.probability,
            "class_label": None if fired is None else fired.class_label,
            "min_contributing_stations": st.MIN_CONTRIBUTING_STATIONS,
        }
        if fired is not None:
            candidates.append(fired)
    receivers = {s.sncl.rsplit(".", 2)[0] for s in source.stations()}
    if candidates:
        best = candidates[0]
        return StageEvidence(
            stage="detection",
            component="M1 discriminator (executed here)",
            outcome=StageOutcome.produced,
            execution=Execution.executed,
            summary=(
                f"candidate {best.detection_id} at {best.detected_at_stream_utc.isoformat()}, "
                f"calibrated p={best.probability}, class {best.class_label}"
            ),
            measured={"modes": modes, "receivers_in_fixture": sorted(receivers)},
        )
    return StageEvidence(
        stage="detection",
        component="M1 discriminator (executed here)",
        outcome=StageOutcome.did_not_fire,
        execution=Execution.executed,
        summary=(
            f"no candidate in either mode: the committed fixture carries {len(receivers)} "
            f"receiver(s) against the detector's minimum of {st.MIN_CONTRIBUTING_STATIONS} "
            "contributing stations, and no window was ever scored"
        ),
        measured={"modes": modes, "receivers_in_fixture": sorted(receivers)},
        blocks_downstream=True,
        notes=[
            "The committed replay fixtures are two vertical-component receivers each -- they "
            "were assembled in Prompt 1 to exercise the streaming plumbing, not to feed a "
            "multi-station discriminator. The M1 build's own waveform set lives under "
            "data/raw/ (DVC-tracked, gitignored) and is not present in a fresh clone.",
        ],
    )


# -- stage 3: LFH -------------------------------------------------------------------------------


def _lfh_stage(repo: Path, event: E2EEvent, *, execute: bool) -> StageEvidence:
    if execute:
        executed = _try_execute_lfh(repo, event)
        if executed is not None:
            return executed
    return lfh_outcome(repo, event.event_id)


def _try_execute_lfh(repo: Path, event: E2EEvent) -> StageEvidence | None:
    """Run M2 offline into a scratch directory. Returns None when it cannot run offline."""
    from serac.cli_lfh import run_inversion

    scratch = Path(tempfile.mkdtemp(prefix="serac-e2e-lfh-"))
    try:
        # run_inversion narrates to stdout; the e2e lane reports through its own artifacts.
        with contextlib.redirect_stdout(io.StringIO()):
            path = run_inversion(
                event.event_id,
                repo=repo,
                offline=True,
                reports_dir=scratch,
                write_report=False,
            )
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (SeracError, OSError, KeyError, ValueError):
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    evidence = lfh_outcome(repo, event.event_id, doc=doc)
    return StageEvidence(
        stage=evidence.stage,
        component=evidence.component + " (executed here, offline)",
        outcome=evidence.outcome,
        execution=Execution.executed,
        summary=evidence.summary,
        artifact_path=None,
        artifact_sha256=None,
        artifact_generated_utc=doc.get("generated_at_utc"),
        measured=evidence.measured,
        blocks_downstream=evidence.blocks_downstream,
        notes=[*evidence.notes, "Re-run in this session from the committed Green's fixtures."],
    )


# -- stages 4-6 ---------------------------------------------------------------------------------


def _not_reached(stage: str, component: str, blocker: StageEvidence) -> StageEvidence:
    return StageEvidence(
        stage=stage,
        component=component,
        outcome=StageOutcome.not_reached,
        execution=Execution.unavailable,
        summary=(
            f"not reached: the {blocker.stage} stage ({blocker.component}) "
            f"{blocker.outcome.value}. Measured reason: {blocker.summary}"
        ),
        blocks_downstream=True,
        notes=[
            "No substitute input was used. A default mass, location or footprint here would "
            "turn an upstream refusal into a forecast.",
        ],
    )


def run_e2e(
    repo: Path,
    event_id: str,
    *,
    reports_dir: Path | None = None,
    write: bool = True,
    execute_lfh: bool = True,
) -> E2EResult:
    """Run the chain for one event and (by default) write `reports/e2e/<event>.{md,json}`."""
    event = EVENTS.get(event_id)
    if event is None:
        raise E2EError(f"unknown e2e event {event_id!r}; known: {', '.join(sorted(EVENTS))}")
    started = datetime.now(tz=UTC)

    stages: list[StageEvidence] = []
    context: list[StageEvidence] = []
    caveats: list[str] = []

    waveform, source = _waveform_stage(repo, event)
    stages.append(waveform)

    if source is None:
        detection = _not_reached("detection", "M1 discriminator", waveform)
    else:
        detection, recorded = _detection_stage(repo, event, source)
        context.append(recorded)
    stages.append(detection)

    # M2 is run regardless of whether M1 fired here, because M1's own recorded run used the
    # full multi-station set and M2's refusal is about station geometry, not about M1. The
    # chain's stopping point is still the first blocking stage in order.
    lfh = _lfh_stage(repo, event, execute=execute_lfh)
    stages.append(lfh)

    blocker = next((s for s in stages if s.blocks_downstream), None)
    if blocker is None:
        raise E2EError(
            "no stage blocked the chain but this lane has no path to a forecast yet: M2 "
            "produced a mass, which the runout stage does not consume in this version"
        )

    stages.append(
        _not_reached("runout", "M4 runout surrogate", lfh if lfh.blocks_downstream else blocker)
    )
    stages.append(_not_reached("cap", "M5 CAP 1.2 generator", blocker))

    exposure = load_exposure(repo, event.aoi_id)
    loss: CascadeLossResult | None = None
    if event.has_frozen_ensemble:
        stats, ensemble = ensemble_arrivals(repo)
        context.append(ensemble)
        forecast = ensemble_prior_forecast(
            repo,
            aoi_id=event.aoi_id,
            event_id=event.event_id,
            stats=stats,
            origin_time_utc=_origin_of(repo, event) or started,
            issued_utc=started,
        )
        request = prior_request(
            forecast,
            exposure,
            request_id=f"e2e-{event.event_id}",
            requested_utc=started,
            lead_time=None,
        )
        loss = compute_avoided_loss(
            request,
            capacities=exposure.capacities,
            rule=ReplacementValueRule(),
            computed_utc=started,
            extra_assumptions=[
                f"The hazard input is {PRIOR_MODEL_NAME}, the frozen ensemble's own arrival "
                "distribution over its Latin-hypercube design prior. It is NOT a forecast of "
                "this event: M2 refused, so no release volume for this event exists.",
                f"The chain stopped at the {blocker.stage} stage. {blocker.summary}",
            ],
        )
        stages.append(
            StageEvidence(
                stage="avoided_loss",
                component="M5 avoided-loss computation",
                outcome=(
                    StageOutcome.produced if loss.computed else StageOutcome.insufficient_input
                ),
                execution=Execution.executed,
                summary=(
                    f"status={loss.response.status}; costed {len(loss.determined_asset_ids)} of "
                    f"{len(exposure.items)} exposed asset(s)"
                ),
                measured={
                    "status": loss.response.status.value,
                    "determined": loss.determined_asset_ids,
                    "undetermined": {k: v.value for k, v in loss.undetermined.items()},
                    "lives_in_warned_zone": None,
                },
                notes=[
                    "Run on the best available input rather than on a forecast, because the "
                    "chain produced no forecast. Every asset it could not cost is reported as "
                    "undetermined, never as zero loss.",
                ],
            )
        )
        caveats.append(
            "The avoided-loss stage was run on the frozen ensemble design prior, out of band "
            "with the chain, so that the exposure and the computation are exercised. It is not "
            "a chain output and must not be read as one."
        )
    else:
        stages.append(_not_reached("avoided_loss", "M5 avoided-loss computation", blocker))
        caveats.append(
            f"No frozen runout ensemble exists for the {event.aoi_id} corridor: M4's ensemble "
            "was built for the Lhende Khola / Trishuli corridor only. There is therefore no "
            "best-available hazard input for this event at all, and the avoided-loss stage has "
            "nothing to run on."
        )

    surrogate_latency = surrogate_latency_s(repo)
    if surrogate_latency is not None:
        caveats.append(
            f"M4's measured surrogate inference latency is {surrogate_latency * 1000:.2f} ms "
            "(p95, CPU, batch 1), from reports/runout/surrogate_metrics.json. It is quoted for "
            "the latency budget only; the surrogate was never invoked in this run."
        )
    case_study = discriminator_case_study(repo, event.event_id)
    if case_study is not None:
        context.append(
            StageEvidence(
                stage="detection-case-study",
                component="M1 discriminator single-window case study",
                outcome=StageOutcome.produced,
                execution=Execution.artifact,
                summary=(
                    f"predicted class {case_study.get('predicted_class')}, calibrated "
                    f"p(mass movement)={case_study.get('calibrated_probability_mass_movement')}"
                ),
                artifact_path=f"reports/m1/case_study_{event.event_id}.json",
                measured={
                    k: case_study.get(k)
                    for k in (
                        "receivers_selected",
                        "receivers_with_response_removed_data",
                        "min_stations_required_by_the_dataset",
                        "below_the_datasets_quality_bar",
                        "class_probabilities",
                    )
                },
                notes=[str(case_study.get("caveat", ""))],
            )
        )

    result = E2EResult(
        event=event,
        started_utc=started,
        finished_utc=datetime.now(tz=UTC),
        serac_version=__version__,
        stages=stages,
        stopped_at=blocker.stage,
        stopped_because=blocker.summary,
        loss=loss,
        exposure=exposure,
        context=context,
        caveats=caveats,
    )
    if write:
        write_e2e_reports(result, reports_dir or (repo / REPORT_SUBDIR))
    return result


def _origin_of(repo: Path, event: E2EEvent) -> datetime | None:
    from serac.pipelines.replay import load_origin

    return (
        load_origin(repo, event.event_id).origin_time_utc
        or load_origin(repo, event.fixture_event_id).origin_time_utc
    )


# -- reporting ----------------------------------------------------------------------------------


def write_e2e_reports(result: E2EResult, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{result.event.event_id}.json"
    json_path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", "utf-8")
    md_path = directory / f"{result.event.event_id}.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds:.1f} s ({timedelta(seconds=round(seconds))})"


def render_markdown(result: E2EResult) -> str:
    from serac.cascade.table import render_loss_table

    event = result.event
    lines: list[str] = [
        f"# End-to-end replay: {event.name}",
        "",
        f"`serac cascade e2e --event {event.event_id}` on serac {result.serac_version}, "
        f"run {result.started_utc.isoformat()}.",
        "",
        "## Verdict",
        "",
        f"**The chain stops at the `{result.stopped_at}` stage.** {result.stopped_because}",
        "",
        "No stage downstream of that point ran, and nothing was substituted for the missing "
        "input. serac produced **no cascade forecast and no CAP alert** for this event.",
        "",
        "## Chain",
        "",
        "| # | Stage | Component | Source | Outcome |",
        "|---|---|---|---|---|",
    ]
    for index, stage in enumerate(result.stages, start=1):
        lines.append(
            f"| {index} | `{stage.stage}` | {stage.component} | {stage.execution.value} | "
            f"**{stage.outcome.value}** |"
        )
    lines += ["", "## Stage detail", ""]
    for stage in result.stages:
        lines.append(f"### `{stage.stage}` — {stage.component}")
        lines.append("")
        lines.append(f"- outcome: **{stage.outcome.value}** ({stage.execution.value})")
        if stage.artifact_path:
            lines.append(
                f"- artifact: `{stage.artifact_path}` "
                f"(sha256 `{(stage.artifact_sha256 or '')[:16]}…`, generated "
                f"{stage.artifact_generated_utc or 'unknown'})"
            )
        lines.append(f"- summary: {stage.summary}")
        if stage.measured:
            lines += ["", "```json", json.dumps(stage.measured, indent=2, sort_keys=True), "```"]
        for note in stage.notes:
            if note.strip():
                lines.append(f"> {note.strip()}")
        lines.append("")
    if result.context:
        lines += ["## Context (not part of the chain)", ""]
        for stage in result.context:
            lines.append(f"### `{stage.stage}` — {stage.component}")
            lines.append("")
            lines.append(f"- outcome: **{stage.outcome.value}** ({stage.execution.value})")
            if stage.artifact_path:
                lines.append(f"- artifact: `{stage.artifact_path}`")
            lines.append(f"- summary: {stage.summary}")
            if stage.measured:
                lines += [
                    "",
                    "```json",
                    json.dumps(stage.measured, indent=2, sort_keys=True),
                    "```",
                ]
            for note in stage.notes:
                if note.strip():
                    lines.append(f"> {note.strip()}")
            lines.append("")
    if result.loss is not None and result.exposure is not None:
        lines += ["## Avoided loss on the best available input", ""]
        lines.append(render_loss_table(result.loss, result.exposure))
        lines.append("")
    if result.caveats:
        lines += ["## Caveats", ""]
        lines += [f"- {c}" for c in result.caveats]
        lines.append("")
    lines += [
        "## What would have to change",
        "",
        "The reason this chain stops is not a defect in the integration. Each stage refused, or "
        "failed to fire, for a measured physical reason that its own report states. Fixing the "
        "integration cannot move any of them.",
        "",
    ]
    return "\n".join(lines) + "\n"
