"""`make validate-e2e`: both replays run to their honest end, and the outputs validate.

The brief's four criteria, and how each is checked:

1. **Both end-to-end replays complete.** "Complete" means the chain ran through every stage
   and each stage reported an outcome -- including `refused` and `not_reached`. A chain that
   stops at a refusal has completed; a chain whose stage could not even be attempted
   (`unavailable`) has not.
2. **A latency report is generated.** `reports/e2e/latency.json`, assembled from the measured
   numbers each component recorded, with the terms it could not measure left null.
3. **CAP validates against the XSD.** Checked twice on the fictional check forecast
   (`serac.alerting.example`): unsigned, then with an Ed25519 enveloped signature appended,
   because a signature that breaks schema validity is a signature that cannot be deployed.
   The real replays produce no CAP message at all, and that is recorded as a warning.
4. **The avoided-loss JSON validates.** Both the real Langtang response (which is an
   `INSUFFICIENT INPUT` response) and a `computed` response over the fictional check request
   are validated against `contracts/avoided-loss-response.v0.json`.

**This suite passes while the chain produces nothing.** That is deliberate and it is the
instruction: an early stop is the outcome to record, not a reason for the harness to fail.
Every early stop is recorded as a non-failing `warning` so it appears in the report, and the
`chain_produced_a_forecast` warning is the one to read first. Do not read a green
`validate-e2e` as evidence that serac forecast anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serac.alerting.example import check_forecast, check_request
from serac.alerting.generator import build_alert
from serac.alerting.keys import generate_keypair
from serac.alerting.signing import verify_cap_signature
from serac.cascade.compute import compute_avoided_loss
from serac.cascade.evidence import Execution, StageOutcome
from serac.domain.avoided_loss import AvoidedLossStatus
from serac.pipelines.e2e import CHAIN_STAGES, EVENTS, E2EResult, run_e2e
from serac.validation.cap import CapValidator
from serac.validation.result import Suite, SuiteResult
from serac.validation.underwriting import (
    RESPONSE_CONTRACT,
    load_contract,
    schema_errors,
)

SUITE_NAME = "e2e"
LATENCY_FILENAME = "latency.json"


def _latency_report(repo: Path, results: list[E2EResult]) -> dict[str, Any]:
    """Every latency term the chain could measure, per event, with nulls where it could not."""
    from serac.cascade.evidence import surrogate_latency_s
    from serac.cascade.prior import issue_delay

    events: dict[str, Any] = {}
    for result in results:
        detection = result.stage("detection")
        lfh = result.stage("lfh")
        delay = issue_delay(repo, result.event.event_id)
        recorded = next((c for c in result.context if c.stage == "detection"), None)
        events[result.event.event_id] = {
            "stopped_at": result.stopped_at,
            "detection_executed_here": {
                "fired": detection is not None and detection.outcome == StageOutcome.produced,
                "modes": (detection.measured.get("modes") if detection else None),
            },
            "detection_recorded_by_m1": (recorded.measured if recorded else None),
            "lfh": {
                "outcome": lfh.outcome.value if lfh else None,
                "execution": lfh.execution.value if lfh else None,
                "wall_clock_s": lfh.measured.get("wall_clock_s") if lfh else None,
            },
            "surrogate_inference_p95_s": surrogate_latency_s(repo),
            "counterfactual_alert_issue_delay_s": delay.total_s,
            "counterfactual_alert_issue_note": delay.as_note(),
            "cap_generation": {
                "emitted": result.cap_identifier is not None,
                "reason": "no forecast exists, so no CAP message was generated",
            },
        }
    return {
        "report_version": "0.1.0",
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "budget_note": (
            "The 180 s detachment-to-CAP figure in docs/ARCHITECTURE.md is a design budget. "
            "Nothing here is evidence for or against it: no CAP message was produced on either "
            "replay, so no end-to-end latency was measured."
        ),
        "events": events,
    }


def run_suite(repo: Path, reports_dir: Path | None = None) -> SuiteResult:
    """Run both replays, write the latency report, and validate the CAP and loss outputs."""
    suite = Suite(SUITE_NAME, repo)
    e2e_dir = repo / "reports" / "e2e"
    results: list[E2EResult] = []

    for event_id in sorted(EVENTS):
        try:
            result = run_e2e(repo, event_id, write=True)
        except Exception as exc:
            suite.check(f"replay_{event_id}", False, f"{type(exc).__name__}: {exc}")
            continue
        results.append(result)
        missing = [s for s in CHAIN_STAGES if result.stage(s) is None]
        unavailable = [
            s.stage
            for s in result.stages
            if s.outcome == StageOutcome.unavailable and s.execution == Execution.unavailable
        ]
        suite.check(
            f"replay_{event_id}_completed",
            not missing and not unavailable,
            (
                f"stages {', '.join(s.stage for s in result.stages)}; stopped at "
                f"{result.stopped_at}"
                + (f"; missing {missing}" if missing else "")
                + (f"; unavailable {unavailable}" if unavailable else "")
            ),
        )
        suite.check(
            f"replay_{event_id}_reports_written",
            (e2e_dir / f"{event_id}.md").exists() and (e2e_dir / f"{event_id}.json").exists(),
            f"reports/e2e/{event_id}.{{md,json}}",
        )
        suite.warn(
            f"replay_{event_id}_chain_produced_a_forecast",
            False,
            (
                f"the chain stopped at the '{result.stopped_at}' stage: {result.stopped_because}"
                " -- no cascade forecast and no CAP alert exist for this event"
            ),
        )
        loss = result.loss
        if loss is not None:
            suite.warn(
                f"replay_{event_id}_assets_costed",
                bool(loss.determined_asset_ids),
                (
                    f"costed {len(loss.determined_asset_ids)} asset(s); "
                    f"{len(loss.undetermined)} undetermined (reported as undetermined, not zero)"
                ),
            )

    # -- latency report ------------------------------------------------------------------------
    e2e_dir.mkdir(parents=True, exist_ok=True)
    latency_path = e2e_dir / LATENCY_FILENAME
    latency_path.write_text(
        json.dumps(_latency_report(repo, results), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    suite.check(
        "latency_report_generated",
        latency_path.exists(),
        f"{latency_path.relative_to(repo)} ({latency_path.stat().st_size} bytes)",
    )

    # -- CAP against the XSD -------------------------------------------------------------------
    xsd = repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"
    if not xsd.exists():
        suite.check("cap_xsd_present", False, f"{xsd} missing")
    else:
        validator = CapValidator(xsd)
        forecast = check_forecast()
        try:
            unsigned = build_alert(forecast, sent=forecast.issued_utc, validator=validator)
            suite.check(
                "cap_unsigned_validates",
                bool(unsigned.message.xml) and not validator.errors(unsigned.message.xml or ""),
                f"{unsigned.message.identifier}: status={unsigned.message.status}, "
                f"{len(unsigned.message.info[0].area)} area block(s)",
            )
            suite.check(
                "cap_status_is_test_for_an_unqualified_tier",
                unsigned.message.status == "Test",
                unsigned.status_rule,
            )
            key = generate_keypair()
            signed = build_alert(
                forecast, sent=forecast.issued_utc, validator=validator, private_key=key
            )
            errors = validator.errors(signed.message.xml or "")
            suite.check(
                "cap_signed_validates",
                signed.signed and not errors,
                f"enveloped Ed25519 XML-Signature; XSD errors: {errors or 'none'}",
            )
            check = verify_cap_signature(signed.message.xml or "", key.public_key())
            suite.check("cap_signature_verifies", check.valid, check.reason)
        except Exception as exc:
            suite.check("cap_generation", False, f"{type(exc).__name__}: {exc}")

    # -- avoided-loss JSON against the contract ------------------------------------------------
    contracts_dir = repo / "contracts"
    try:
        schema = load_contract(contracts_dir, RESPONSE_CONTRACT)
    except FileNotFoundError as exc:
        suite.check("avoided_loss_contract_present", False, str(exc))
        return suite.result()

    computed = compute_avoided_loss(check_request())
    errors = schema_errors(schema, computed.response.model_dump(mode="json"))
    suite.check(
        "avoided_loss_computed_response_validates",
        computed.response.status == AvoidedLossStatus.computed and not errors,
        (
            f"status={computed.response.status}, {len(computed.response.losses)} scenario "
            f"loss(es), {len(computed.determined_asset_ids)} asset(s) costed on the fictional "
            f"check request; schema errors: {errors or 'none'}"
        ),
    )
    for result in results:
        if result.loss is None:
            continue
        payload = result.loss.response.model_dump(mode="json")
        errors = schema_errors(schema, payload)
        suite.check(
            f"avoided_loss_{result.event.event_id}_validates",
            not errors,
            (f"status={result.loss.response.status}; schema errors: {errors or 'none'}"),
        )
    suite.info(
        "how_to_read_this_suite",
        "Every check above is about machinery. The warnings are about results: on both "
        "replays the chain stopped before a forecast, so serac issued no alert and costed no "
        "asset from a forecast. A green validate-e2e means the lane is wired and honest, not "
        "that it forecast anything.",
    )
    return suite.result()
