# ADR-0014: portable job manifests for scaled compute; plain Docker image as the deployment unit

Date: 2026-09-03

## Status

Accepted

## Context

Non-negotiable 6: local dev is Docker Compose; scaled runs (InSAR batches, cube builds,
later ensembles and training) are job manifests with core-hour and storage estimates,
written for a generic container host and annotated for AWS Batch / EC2 GPU, with no
managed-platform lock-in.

## Decision

- The deployment unit is one plain Docker image containing the `serac` package.
- `infra/docker/compose.yaml` provides the dev dependencies (Redis now; GRASS placeholder
  for Prompt 2).
- `infra/jobs/*.yaml` use a small generic schema (`image, command, env, resources{cpu,
  memory, gpu, storage}, estimated_core_hours{low, high, basis}, inputs/outputs by DVC path`)
  plus an `aws:` annotation block (Batch job-definition sketch, instance-family suggestion).
  The schema is documented in `infra/jobs/README.md`. Nothing in `src/` reads these files.
- Core-hour figures are ranges with the estimate basis stated; they are not measurements
  until a run has been recorded.

## Consequences

- Any container host can run a job from the generic fields; AWS annotations are hints.
- Neither Compose nor the manifests were executed on the dev machine (no Docker);
  `RELEASE_STATUS.md` records that.
