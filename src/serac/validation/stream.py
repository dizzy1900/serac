"""`validate-stream`: the real-time lane runs end to end on committed fixtures, offline.

Checks (errors unless stated):

* every `data/fixtures/seismic/<event>/manifest.json` re-hashes clean;
* the CAP vendor `MANIFEST.json` checksums match the vendored XSDs;
* `detector_stub.py` starts with the STUB marker, and its config says the threshold is a
  placeholder;
* replay of `chamoli-2021` at speed `max` on the in-memory bus completes with
  `pending_after_drain == 0`, `chunks_consumed == chunks_published`, and a report that
  validates against `contracts/replay-report.v0.json`;
* the same for `langtang-2026` when its fixture is fetched, else a **warning**;
* the synthetic `synthetic-lp-burst` lane yields at least one detection and at least one CAP
  message that validates against the CAP 1.2 XSD;
* the golden ratio record for the Chamoli fixture matches.

Whether the stub fires on the real fixtures is recorded as `info`, never asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from serac.domain import topics
from serac.domain.cap import CAPMessage
from serac.domain.replay import FixtureManifest, ReplayReport
from serac.domain.schema_export import contract_filename
from serac.errors import SeracError
from serac.pipelines.replay import ReplayConfig, run_replay
from serac.streaming.detector_stub import STUB_MARKER, DetectorStubConfig
from serac.streaming.golden import compute_golden, diff_golden, golden_path, load_golden
from serac.streaming.replay_source import (
    SYNTHETIC_EVENT_ID,
    FixtureIntegrityError,
    FixtureNotFetchedError,
    fixture_dir_for,
    load_fixture_manifest,
    verify_fixture,
)
from serac.validation.cap import CapValidator, verify_vendor_manifest
from serac.validation.result import Suite, SuiteResult

SUITE_NAME = "stream"
REQUIRED_EVENT = "chamoli-2021"
OPTIONAL_EVENTS = ("langtang-2026",)


def _report_schema_errors(repo: Path, report: ReplayReport) -> list[str]:
    path = repo / "contracts" / contract_filename("replay-report")
    if not path.exists():
        return [f"{path} missing; run `serac schema export`"]
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    instance = json.loads(report.model_dump_json())
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    ]


def _check_fixture(suite: Suite, repo: Path, event_id: str, *, required: bool) -> bool:
    fixture_dir = fixture_dir_for(repo, event_id)
    name = f"fixture_hashes:{event_id}"
    try:
        manifest: FixtureManifest = load_fixture_manifest(fixture_dir)
        refs = verify_fixture(fixture_dir, manifest)
    except FixtureNotFetchedError as exc:
        if required:
            suite.check(name, False, str(exc))
        else:
            suite.warn(name, False, f"not fetched: {exc}")
        return False
    except FixtureIntegrityError as exc:
        suite.check(name, False, str(exc))
        return False
    suite.check(name, True, f"{len(refs)} files re-hashed; status {manifest.status}")
    return True


def _replay_checks(
    suite: Suite, repo: Path, report_dir: Path, event_id: str, *, real: bool
) -> ReplayReport | None:
    config = ReplayConfig(
        event_id=event_id,
        speed="max",
        chunk_seconds=5.0,
        bus="in_memory",
        report_dir=report_dir,
        repo_root=repo,
        detector=DetectorStubConfig(allow_synthetic=not real),
    )
    try:
        report = run_replay(config)
    except SeracError as exc:
        suite.check(f"replay_completes:{event_id}", False, f"{type(exc).__name__}: {exc}")
        return None
    counts = report.counts
    suite.check(
        f"replay_completes:{event_id}",
        report.status == "completed",
        f"status={report.status} error={report.error!r} "
        f"run={report.wall_clock_latencies.total_run_s:.2f}s",
    )
    suite.check(
        f"replay_pending_zero:{event_id}",
        counts.pending_after_drain == 0,
        f"pending_after_drain={counts.pending_after_drain}",
    )
    suite.check(
        f"replay_consumed_all:{event_id}",
        counts.chunks_consumed == counts.chunks_published,
        f"consumed {counts.chunks_consumed} of {counts.chunks_published} published",
    )
    suite.check(
        f"replay_is_stub:{event_id}",
        report.is_stub
        and report.detector.is_stub
        and any(STUB_MARKER in c for c in report.caveats),
        "report says is_stub and carries the STUB caveat",
    )
    errors = _report_schema_errors(repo, report)
    suite.check(
        f"replay_report_validates:{event_id}",
        not errors,
        "matches contracts/replay-report" if not errors else "; ".join(errors[:5]),
    )
    suite.check(
        f"replay_wall_latencies_invalid_at_max:{event_id}",
        report.wall_clock_latencies.valid is False,
        "wall-clock latencies flagged invalid at speed max",
    )
    if real:
        suite.check(
            f"replay_no_synthetic:{event_id}",
            not report.contains_synthetic,
            "fixtures are real",
        )
        suite.info(
            f"stub_fired_observation:{event_id}",
            f"detections={counts.detections_emitted} cap={counts.cap_messages_emitted} "
            f"(observation at placeholder threshold {report.detector.params.get('threshold')}; "
            "not a target)",
        )
    return report


def run_suite(repo: Path, report_dir: Path | None = None) -> SuiteResult:
    """Run every stream check; replay reports go to `report_dir` (default reports/replay)."""
    suite = Suite(SUITE_NAME, repo)
    report_dir = report_dir if report_dir is not None else repo / "reports" / "replay"

    # Detector docstring marker.
    detector_path = repo / "src" / "serac" / "streaming" / "detector_stub.py"
    text = detector_path.read_text(encoding="utf-8") if detector_path.exists() else ""
    suite.check(
        "detector_docstring_stub_marker",
        text.startswith(f'"""{STUB_MARKER}'),
        f"{detector_path.name} first docstring line is {STUB_MARKER!r}",
    )
    suite.check(
        "detector_threshold_is_placeholder",
        DetectorStubConfig().as_params().get("threshold_is_placeholder") is True,
        f"threshold={DetectorStubConfig().threshold} declared placeholder",
    )

    # CAP vendor schema checksums.
    problems = verify_vendor_manifest(repo / "contracts" / "vendor" / "cap")
    suite.check("cap_xsd_vendor_manifest", not problems, "; ".join(problems) or "checksums match")
    try:
        validator: CapValidator | None = CapValidator(
            repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"
        )
    except (FileNotFoundError, Exception) as exc:  # lxml raises XMLSchemaParseError
        suite.check("cap_xsd_compiles", False, str(exc))
        validator = None
    else:
        suite.check("cap_xsd_compiles", True, "CAP-v1.2.xsd compiles without a resolver")

    # Fixtures and replays.
    if _check_fixture(suite, repo, REQUIRED_EVENT, required=True):
        chamoli = _replay_checks(suite, repo, report_dir, REQUIRED_EVENT, real=True)
        if chamoli is not None:
            golden = golden_path(repo, REQUIRED_EVENT)
            if golden.exists():
                diff = diff_golden(load_golden(golden), compute_golden(repo, REQUIRED_EVENT))
                suite.check(
                    f"golden_ratios:{REQUIRED_EVENT}",
                    not diff,
                    "; ".join(diff[:3]) or f"matches {golden.relative_to(repo)}",
                )
            else:
                suite.warn(f"golden_ratios:{REQUIRED_EVENT}", False, f"{golden} missing")
    for event_id in OPTIONAL_EVENTS:
        if _check_fixture(suite, repo, event_id, required=False):
            _replay_checks(suite, repo, report_dir, event_id, real=True)

    # Synthetic lane: the guaranteed detection -> CAP path.
    synthetic = _replay_checks(suite, repo, report_dir, SYNTHETIC_EVENT_ID, real=False)
    if synthetic is not None:
        suite.check(
            "synthetic_lane_labelled",
            synthetic.contains_synthetic
            and all(f.provenance == "synthetic" for f in synthetic.fixtures)
            and not any(f.path.startswith("data/") for f in synthetic.fixtures),
            "fixtures labelled synthetic; nothing under data/",
        )
        suite.check(
            "synthetic_lane_detects",
            synthetic.counts.detections_emitted >= 1,
            f"detections={synthetic.counts.detections_emitted}",
        )
        suite.check(
            "synthetic_lane_caps",
            synthetic.counts.cap_messages_emitted >= 1,
            f"cap_messages={synthetic.counts.cap_messages_emitted}",
        )
        if validator is not None:
            _cap_xsd_check(suite, repo, validator)
    return suite.result()


def _cap_xsd_check(suite: Suite, repo: Path, validator: CapValidator) -> None:
    """Re-run the synthetic lane on a bus we can read back and validate every CAP message."""
    from serac.adapters.bus.in_memory import InMemoryBus

    bus = InMemoryBus()
    config = ReplayConfig(
        event_id=SYNTHETIC_EVENT_ID,
        speed="max",
        bus="in_memory",
        repo_root=repo,
        detector=DetectorStubConfig(allow_synthetic=True),
    )
    run_replay(config, bus=bus, write_report=False)
    alerts = bus.log(topics.ALERTS)
    valid = 0
    problems: list[str] = []
    for envelope in alerts:
        message = envelope.payload
        if not isinstance(message, CAPMessage) or message.xml is None:
            problems.append(f"{envelope.message_id}: no CAP XML")
            continue
        errors = validator.errors(message.xml)
        if errors:
            problems.extend(errors)
        else:
            valid += 1
        if (
            message.status != "Test"
            or message.scope != "Private"
            or any(info.area for info in message.info)
        ):
            problems.append(f"{message.identifier}: not Test/Private/no-area")
    suite.check(
        "synthetic_cap_validates_xsd",
        valid >= 1 and not problems,
        f"{valid} of {len(alerts)} CAP messages valid"
        + ("; " + "; ".join(problems[:3]) if problems else ""),
    )
