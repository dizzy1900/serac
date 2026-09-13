# ADR-0017: Detection-path CAP stays Test; CapStub remains selectable

Date: 2026-09-13

## Status

Accepted

## Context

Audit finding A9 (`reports/AUDIT_2026-09-04.md`) and Deferred item 4 in the release ledger
recorded that `src/serac/alerting/generator.py` already produced XSD-valid CAP 1.2 (used by
`serac cascade e2e`) while `src/serac/pipelines/replay.py` and `src/serac/cli_stream.py` still
constructed `CapStub`. Detection-path messages on that stub were `status=Test`, which is the
honest status while no detector is validated. Replacing the stub in the lane must not turn
those messages into `Actual`, invent geometry, or claim the ≤ 180 s design budget is proven.

ADR-0012 remains in force for the stub form. This decision is about which renderer the live
and replay lanes call by default.

## Decision

- The default CAP stage on replay and `serac stream run cap` is
  `src/serac/streaming/cap_stage.py`. It renders through `serac.adapters.cap.cap12.render`
  (the same XSD path as the forecast generator) and optionally signs with Ed25519 when
  `SERAC_CAP_SIGNING_KEY` names a readable key. A missing key yields unsigned valid CAP; the
  lane does not fail closed.
- `DetectionCandidate` inputs map to `status=Test`, `scope=Private`, Unknown
  urgency/severity/certainty, and **no `area`**. Instruction text states this is research
  output, not an operational alert. Helpers from `serac.alerting.generator` that do not
  require a `CascadeForecast` are reused; `severity_for`, `urgency_for`, and `area_for` are
  not, because those derive arrival times, polygons, and severity a detection does not have.
  `DetectionCandidate.source_location` may be recorded as a parameter; it is never turned
  into a CAP polygon or circle.
- `CascadeForecast` inputs call `build_alert`, so `STATUS_BY_TIER` applies. That table is
  not changed: `high -> Actual` remains unreachable given current model maturity.
- `CapStub` stays in the tree. `--cap stub` selects it. Golden-ratio tests and any fixture
  that pins stub XML keep using it.
- The CLI prints that `status=Test` and that this is not an alert system.

## Consequences

- `validate-stream` XSD-checks default-lane output from the real renderer, still as
  Test/Private/no-area on the detection path.
- Selecting a trained detector still does not make the lane an alert system.
- Replay report shape is unchanged (no new contract fields). Committed `reports/replay/`
  artefacts are not rewritten; they remain plumbing records from earlier stub runs.
