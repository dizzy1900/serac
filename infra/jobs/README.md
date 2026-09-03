# infra/jobs — portable job manifests for scaled runs

Scaled work (InSAR batches over an archive, feature-cube builds over years of imagery,
Sentinel-1 stack processing, and in Prompt 2 simulation ensembles and model training) is
described here as container-job manifests written for a **generic container host** and
annotated for **AWS Batch / EC2** as the assumed target (non-negotiable 6, ADR-0014). No
code under `src/` reads these files; they are the operator-facing description of how to run
serac at scale without locking into a managed platform.

**None of these manifests has been executed.** Core-hour and storage figures are estimates
with their basis stated in each file; they become measurements only when a run is recorded
(add an `observed:` block with the run id, date and actual numbers, and keep the estimate
for comparison).

## Manifests

| File | Job | Needs credentials |
|---|---|---|
| `hyp3-insar-batch.yaml` | submit, watch and download a batch of HyP3 InSAR pairs for an AOI and date range; ledger every product | Earthdata Login |
| `cube-build.yaml` | build (or rebuild) the Zarr feature cube and STAC catalog for one AOI from `data/raw` | none (reads DVC-pulled data) |
| `s1-stack.yaml` | list and download a Sentinel-1 SLC/GRD stack for an AOI (the > 5 GB gate applies) | Earthdata Login |

## Schema

Every manifest is a YAML document with these top-level keys.

| Key | Meaning |
|---|---|
| `apiVersion` | `serac.jobs/v0` |
| `kind` | `ContainerJob` |
| `metadata.name`, `metadata.description`, `metadata.owner_role` | identity; `owner_role` is one of the subagent roles in `CLAUDE.md` |
| `metadata.status` | `designed`, `executed`, `measured` — mirrors `RELEASE_STATUS.md` |
| `image` | the serac deployment image reference; `<registry>/serac:<git-sha>` until a registry is chosen |
| `command` | argv list; always a `serac …` sub-command so the job is reproducible from the CLI |
| `env` | list of `{name, from}` where `from` is `secret:<NAME>` (injected from a secret store, never committed) or `value:<literal>` |
| `parameters` | job inputs the operator sets (AOI id, date range); referenced as `${param}` in `command` |
| `resources.cpu`, `resources.memory_gb`, `resources.gpu`, `resources.storage_gb` | per-task request; `gpu` is `0` for every Prompt 1 job |
| `parallelism` | how the job shards (array size and the sharding key) |
| `estimated_core_hours` | `{low, high, basis}`; `basis` states the assumption the range rests on |
| `estimated_storage_gb` | `{low, high, basis}` |
| `inputs`, `outputs` | DVC paths under `data/` (and `reports/`); the job runs `dvc pull` on inputs and the operator runs `dvc push` on outputs |
| `ledger` | which `data/manifest.jsonl` entries the job appends (source, status) |
| `preconditions` | human checks that must be true before submission (credentials, ask-first gates) |
| `aws` | annotation only: a Batch job-definition sketch and an instance-family suggestion. Any container host can ignore this block. |

Rules:

- A job never writes under `data/` without appending ledger entries, exactly as the CLI does.
- Any job whose download estimate exceeds 5 GB needs an explicit operator confirmation
  (`preconditions`), matching the CLI gate.
- Credentials come from the host's secret store (`from: secret:…`); nothing here holds a value.
- GPU jobs (Prompt 2 training, surrogate ensembles) will add `resources.gpu` and an
  `aws.instance_family` in the `g`/`p` families; none exist yet.

## Submitting on AWS Batch (sketch)

The `aws.batch_job_definition` block in each manifest is a sketch, not a deployable
document: register a job definition from it with the image, vCPU and memory filled in, create
a compute environment in the suggested instance family, then submit with the `parameters`
as Batch parameters. Alternatives (a plain EC2 instance running `docker run`, any Kubernetes
job, a workstation) use the generic fields only.
