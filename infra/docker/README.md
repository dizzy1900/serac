# infra/docker — the deployment image and local development dependencies

| File | Purpose | Status |
|---|---|---|
| `Dockerfile` | **the deployment unit** (ADR-0014): one plain image containing the `serac` package | built and exercised 2026-09-04 on `linux/arm64`; **never pushed to any registry** |
| `compose.yaml` | dev dependencies for the streaming lane (Redis; commented GRASS placeholder) | file written, **never brought up** (`RELEASE_STATUS.md` Known gap 60) |
| `ravaflow/README.md` | the record that r.avaflow could not be obtained; there is nothing to build | acquisition **failed**, dated and sourced |

## The deployment image

Build from the **repository root**, not from this directory — the build context is the repo:

```bash
docker build -f infra/docker/Dockerfile -t serac:$(git rev-parse --short HEAD) .
```

The image that the GPU manifests call `…-cuda` is the same file with the ML extras:

```bash
docker build -f infra/docker/Dockerfile \
  --build-arg EXTRAS="--extra ml --extra surrogate" \
  -t serac:$(git rev-parse --short HEAD)-cuda .
```

There is no second Dockerfile because there is no second image to describe: the locked `torch`
wheel already depends on the CUDA runtime under `sys_platform == 'linux'` (see `uv.lock`), so the
GPU variant differs only by which optional dependency groups are installed. The NVIDIA driver
and container toolkit come from the host; an image cannot supply them.

Run it. The entrypoint is the CLI itself, so a container takes `serac` sub-command arguments:

```bash
docker run --rm serac:<tag> --version
docker run --rm -v "$PWD:/work" serac:<tag> validate events
```

`WORKDIR` is `/work` and the process runs as uid 10001, not root. Mount the repository there:
the jobs in `infra/jobs/` append to `data/manifest.jsonl` exactly as the CLI does, and a ledger
written as uid 0 is one the operator cannot append to afterwards.

### What is guaranteed, and what is not

Every install in the image goes through `uv sync --frozen`, which fails rather than re-resolving
when `uv.lock` and `pyproject.toml` have drifted. An image therefore cannot contain a dependency
set the repository has not locked. `tests/unit/test_deployment_image.py` asserts that property,
the `serac` entrypoint, the Python pin and the build context against this file, offline.

**No image has been pushed to any registry** (`RELEASE_STATUS.md` Known gap 68). The
`infra/jobs/*.yaml` manifests name the unresolvable placeholder `<registry>/serac:<git-sha>` on
purpose: an operator who copies it gets an obvious failure rather than a `manifest unknown` from
a registry that was never written to. Build and tag your own.

### Build record — 2026-09-04

Measured, not estimated. Repeat it and correct this table rather than trusting it.

| | |
|---|---|
| Command | `docker build -f infra/docker/Dockerfile -t serac:local-verify .` |
| Host | macOS arm64, Docker Engine 29.7.2, containerd image store |
| Platform built | `linux/arm64` only — **`linux/amd64` has never been built**, and the AWS annotations in `infra/jobs/` assume it |
| Extras | none (base image); the `--extra ml --extra surrogate` variant has **not** been built |
| Result | exit 0 |
| Size | 394,200,414 bytes by `docker image inspect --format '{{.Size}}'`; `docker images` prints `1.75GB` for the same image (the two count compressed and uncompressed layers respectively) |
| `docker run --rm serac:local-verify --version` | `serac 0.1.0` |
| `docker run --rm -v "$PWD:/work:ro" serac:local-verify schema export --check --out /work/contracts` | `22 contracts up to date` — the code in the image reproduces the committed `contracts/` byte for byte |
| Not exercised | every sub-command that needs `data/`, credentials or the network; the ML extras; multi-arch |

Two things the first build found, both fixed in the Dockerfile and both invisible without
building: `obspy` 1.5.1 publishes no manylinux **aarch64** wheel and is compiled from its sdist
(the build stage installs a C toolchain, which does not reach the runtime stage), and
`python:3.12-slim-bookworm` ships no `libexpat1`, which rasterio's bundled GDAL links — without
it every `serac` sub-command died on `import rasterio`.

## Local development dependencies (`compose.yaml`)

`compose.yaml` provides the services the serac streaming lane needs locally:

| Service | Image | Purpose | Status |
|---|---|---|---|
| `redis` | `redis:7-alpine` | Redis Streams bus (`RedisStreamsBus`, ADR-0007); named volume `serac-redis-data`; healthcheck `redis-cli ping` | file written, **not run** |
| `grass` | — | GRASS GIS placeholder for Prompt 2; commented out | placeholder |

**This compose file is untested.** It was written on a machine with no Docker installed. Docker
is available there now — the deployment image above was built and run on it on 2026-09-04 — but
`compose.yaml` itself has still never been brought up, and the Redis bus adapter is still
unit-tested only against `fakeredis`; the live-server test (`redis` marker) has never run
(`RELEASE_STATUS.md` Known gap 60). The first person to run the steps below should flip the
`RedisStreamsBus` and `Docker Compose` rows in `RELEASE_STATUS.md` on success.

### Bring-up

```bash
docker compose -f infra/docker/compose.yaml up -d
docker compose -f infra/docker/compose.yaml ps        # redis should be "healthy"
```

Redis is bound to `127.0.0.1:6379` only. Persistence is append-only (`--appendonly yes`) on
the named volume so stream contents survive a restart; `docker compose down -v` wipes it.

### Running the online / Redis tests against it

```bash
cp .env.example .env                       # if you have not already
# SERAC_REDIS_URL defaults to redis://localhost:6379/0 in .env.example
SERAC_REDIS_URL=redis://localhost:6379/0 make smoke-online
```

`make smoke-online` sets `SERAC_ONLINE=1` and runs tests marked `online` or `redis`. The
`redis`-marked tests are collected only when `SERAC_REDIS_URL` is set and the server answers
`PING` (`tests/conftest.py`); otherwise they skip. Network-dependent `online` tests skip when
their host is unreachable. Skips are a valid outcome and never turn CI red.

To run the replay against the Redis bus instead of the in-memory bus (planned CLI flag):

```bash
uv run serac replay --event chamoli-2021 --speed max --bus redis
```

### Tear-down

```bash
docker compose -f infra/docker/compose.yaml down        # keep the volume
docker compose -f infra/docker/compose.yaml down -v     # drop stream data
```
