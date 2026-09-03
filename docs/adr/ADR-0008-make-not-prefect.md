# ADR-0008: plain `make` for orchestration; prefect not introduced

Date: 2026-09-03

## Status

Accepted

## Context

Local orchestration needs a handful of reproducible entry points (lint, test, validate,
promote, replay). The brief states that `prefect` is not introduced in Prompt 1.

## Decision

- `Makefile` is the orchestration layer for local work: `sync, lint, typecheck, test,
  smoke-online, validate-events, validate-ingest, validate-cube, validate-stream,
  validate-serac, promote, underwriting-check, replay, dvc-remote, clean`.
- Each target delegates to a `serac` sub-command or a `uv run` tool; no logic lives in make
  beyond dependency order and environment guards.
- DVC stages (`dvc.yaml`) describe the data pipeline; scaled runs are described by
  `infra/jobs/*.yaml` (ADR-0014). No workflow engine.

## Consequences

- No scheduler, no retries, no UI. Acceptable for Prompt 1.
- Introducing prefect (or any scheduler) later requires a new ADR and must not change the
  `make` interface that CI and the docs rely on.
