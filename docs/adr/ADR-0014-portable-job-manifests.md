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

### Amended 2026-09-04 — the image is built by a file in the tree, and an image reference is a claim

The decision above named a deployment image without putting the means to build one in the
tree, and three Prompt 2 manifests then named a concrete tag (`ghcr.io/…/serac:0.1.0`) that
nobody had ever pushed. Two rules close that:

- **`infra/docker/Dockerfile` is the only way a serac image is built.** Multi-stage, `python:
  3.12-slim-bookworm`, every install through `uv sync --frozen` so an image can never contain a
  dependency set `uv.lock` does not describe, non-root, `ENTRYPOINT ["serac"]`. Optional
  dependency groups are a build argument (`EXTRAS`), not a second Dockerfile: the GPU jobs' `-cuda`
  variant is this image plus `--extra ml --extra surrogate`, because the locked `torch` wheel
  already carries the CUDA runtime on Linux and the driver comes from the host.
- **A resolvable image reference asserts that the image exists, so it carries its provenance.**
  Until an image is pushed, `infra/jobs/*.yaml` name the unresolvable placeholder
  `<registry>/serac:<git-sha>`; a concrete reference is admissible only with an
  `image_published:` block (`registry`, `digest`, `pushed_utc`, `pushed_by`) beside it. This is
  the manifests' existing `basis:` rule applied to the image: a claim about the world needs its
  evidence next to it. `tests/unit/test_deployment_image.py` enforces both rules, along with the
  weaker one that a repository path named in an `infra/` file has to exist.

## Consequences

- Any container host can run a job from the generic fields; AWS annotations are hints.
- The manifests were never executed, and no image has been pushed to any registry, so no job
  can be submitted without first building and tagging the image locally. `RELEASE_STATUS.md`
  records both (Known gaps 61 and 68).
- Compose remains unexecuted (Known gap 60).
