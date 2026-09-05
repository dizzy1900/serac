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
| `hyp3-insar-batch.yaml` | submit a batch of HyP3 InSAR pairs for an AOI and date range, `--wait` for them and download them; ledger every product. `followup_command` (`--poll`) is the unattended half | Earthdata Login |
| `cube-build.yaml` | build (or rebuild) the Zarr feature cube and STAC catalog for one AOI from `data/raw` | none (reads DVC-pulled data) |
| `s1-stack.yaml` | list and download a Sentinel-1 SLC/GRD stack for an AOI (the > 5 GB gate applies) | Earthdata Login |
| `m2-greens-library.yaml` | pre-build the Syngine Green's-function library M2's warm path needs | none (public Syngine) |
| `runout-ensemble-10k.yaml` | a 10⁴-member `serac-swe-voellmy` Latin-hypercube ensemble over the corridor | none |
| `fno-train-gpu.yaml` | train the M4 corridor surrogate on a GPU host | none |
| `discriminator-train-deep.yaml` | train the M1 station-axis transformer at full scale and re-score the sealed test folds | none |

## Schema

Every manifest is a YAML document with these top-level keys.

| Key | Meaning |
|---|---|
| `apiVersion` | `serac.jobs/v0` |
| `kind` | `ContainerJob` |
| `metadata.name`, `metadata.description`, `metadata.owner_role` | identity; `owner_role` is one of the subagent roles in `CLAUDE.md` |
| `metadata.status` | `designed`, `executed`, `measured` — mirrors `RELEASE_STATUS.md` |
| `image` | the serac deployment image reference. **No serac image has been pushed to any registry**, so this is the unresolvable placeholder `<registry>/serac:<git-sha>`: build and tag your own from `infra/docker/Dockerfile` (see `infra/docker/README.md`). A concrete reference is admissible only with an `image_published:` block beside it |
| `command` | argv list; always a `serac …` sub-command so the job is reproducible from the CLI. `tests/unit/test_job_manifests.py` resolves it against the installed CLI — the sub-command, every flag and every parameter value must parse, or the test fails |
| `followup_command` | optional second argv for the unattended half of a lifecycle (`hyp3-insar-batch.yaml`'s `--poll`). Checked exactly like `command` |
| `env` | list of `{name, from}` where `from` is `secret:<NAME>` (injected from a secret store, never committed) or `value:<literal>` |
| `parameters` | job inputs the operator sets (AOI id, date range); referenced as `${param}` in `command` |
| `resources.cpu`, `resources.memory_gb`, `resources.gpu`, `resources.storage_gb` | per-task request; `gpu` is `0` for every Prompt 1 job |
| `parallelism` | how the job shards (array size and the sharding key), and any argv the shard appends (`shard_argv`) |
| `estimated_core_hours` | `{low, high, basis}`; `basis` states the assumption the range rests on |
| `estimated_storage_gb` | `{low, high, basis}` |
| `inputs`, `outputs` | DVC paths under `data/` (and `reports/`); the job runs `dvc pull` on inputs and the operator runs `dvc push` on outputs |
| `ledger` | which `data/manifest.jsonl` entries the job appends (source, status) |
| `preconditions` | human checks that must be true before submission (credentials, ask-first gates) |
| `aws` | annotation only: a Batch job-definition sketch and an instance-family suggestion. Any container host can ignore this block. Its `command` is the generic `command` with `${param}` written as `Ref::param` and nothing else changed — a test enforces that, so the sketch cannot drift from the job |

Rules:

- A job never writes under `data/` without appending ledger entries, exactly as the CLI does.
- **A `command` is argv the CLI accepts, not a proposal.** It resolves against `serac --help`
  with the manifest's own `parameters` substituted in, and the `aws:` sketch is the same argv.
  When a manifest and the CLI disagree, the manifest is what changes. A manifest may not hedge
  that its flags are unreconciled: the check runs on every `make test`.
- **The entrypoint is exec-form** (`ENTRYPOINT ["serac"]`), so no shell expands anything. The
  only substitution allowed in an argv is `${param}` (or `Ref::param` in the aws sketch) naming
  a declared parameter; `$(nproc)` and `$HOME` would reach the CLI as literal arguments.
- **An ingest job carries `--yes`** (or `--poll`/`--receive`, which take neither). With neither
  `--dry-run` nor `--yes`, `serac ingest` exits 2; with `--dry-run` it prints a plan and writes
  nothing at all, which is not a scaled run.
- Any job whose download estimate exceeds 5 GB needs an explicit operator confirmation
  (`preconditions`), matching the CLI gate. That gate asks on **stdin**: a task whose estimate
  crosses it, or whose size is unknown (HyP3 publishes none), cannot complete unattended.
  Shard it until one task's estimate is under the gate, or attach a stdin and answer.
- Credentials come from the host's secret store (`from: secret:…`); nothing here holds a value.
- An image reference is a claim that the image exists. Until one is pushed, every manifest
  names the placeholder; a resolvable tag needs `image_published: {registry, digest,
  pushed_utc, pushed_by}` in the same mapping, the same way a cost figure needs its
  `basis:`. `tests/unit/test_deployment_image.py` fails otherwise, and also fails if a
  manifest names a repository path that does not exist.
- GPU jobs add `resources.gpu` and an `aws.instance_family` in the `g`/`p` families:
  `fno-train-gpu.yaml` and `discriminator-train-deep.yaml` do.

## Submitting on AWS Batch (sketch)

The `aws.batch_job_definition` block in each manifest is a sketch, not a deployable
document: register a job definition from it with the image, vCPU and memory filled in, create
a compute environment in the suggested instance family, then submit with the `parameters`
as Batch parameters. Alternatives (a plain EC2 instance running `docker run`, any Kubernetes
job, a workstation) use the generic fields only.
