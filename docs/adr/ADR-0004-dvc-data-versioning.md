# ADR-0004: DVC for data versioning; `data/manifest.jsonl` is not a DVC output

Date: 2026-09-03

## Status

Accepted

## Context

Raw, interim and feature data are large and must be reproducible; the provenance ledger
must be small, git-diffable, and appended by many stages and adapters. There is no `dvc`
binary on the dev machine; DVC 3.67 is installed as a uv dependency, so `uv run dvc …` works.

## Decision

- DVC tracks `data/raw`, `data/interim`, `data/features` and `baselines` (gitignored).
  Committed to git: `data/events`, `data/aoi`, `data/fixtures`, `data/manifest.jsonl`,
  `contracts`, `dvc.yaml`, `dvc.lock`, `.dvc/config` (no URL), `.dvcignore`.
- The remote is configured from the environment: `make dvc-remote` writes `$DVC_REMOTE_URL`
  into the gitignored `.dvc/config.local`. No remote URL is ever committed. The optional
  `s3` extra installs `dvc-s3`.
- `data/manifest.jsonl` is **deliberately not a DVC output**. Multiple stages append to it;
  declaring it an `out` of any one stage would make DVC delete or overwrite it. It is a
  plain git-tracked file, append-only, validated by `make validate-ingest`.
- Credentialed ingest stages are marked `frozen: true` so `dvc repro` never triggers a
  network fetch by accident. CI never pulls from the remote; it runs `dvc stage list` to
  prove the pipeline parses.

## Consequences

- A fresh clone with no remote still passes `make validate-serac` on committed fixtures.
- The ledger can drift from the DVC cache if someone deletes cached files by hand; the
  re-hash in `validate-ingest` catches that.
