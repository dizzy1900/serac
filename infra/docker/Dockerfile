# serac deployment image — the deployment unit (ADR-0014).
#
# One plain Docker image containing the `serac` package and nothing else: no orchestrator, no
# managed-platform agent, no entrypoint shell script. `infra/jobs/*.yaml` name this image and
# then run an ordinary `serac …` sub-command, so anything a job can do is something an operator
# can reproduce locally with `uv run serac …`.
#
# Build from the REPOSITORY ROOT, not from this directory:
#
#   docker build -f infra/docker/Dockerfile -t serac:$(git rev-parse --short HEAD) .
#
# The runtime extras the ML jobs need are a build argument, not a second Dockerfile:
#
#   docker build -f infra/docker/Dockerfile --build-arg EXTRAS="--extra ml --extra surrogate" \
#     -t serac:$(git rev-parse --short HEAD)-cuda .
#
# On linux/amd64 and linux/arm64 the locked `torch` wheel already carries the CUDA runtime
# libraries (see `uv.lock`: torch depends on `cuda-toolkit` under `sys_platform == 'linux'`), so
# the `-cuda` variant differs from the base image only by those extras. It still needs an NVIDIA
# driver and the container toolkit on the host; the image cannot supply those.
#
# Reproducibility: every dependency resolution goes through `uv sync --frozen`, which fails
# rather than re-resolving if `uv.lock` and `pyproject.toml` have drifted. An image can
# therefore never contain a dependency set that the repository has not locked.
# `tests/unit/test_deployment_image.py` asserts that property against this file.
#
# Build status is recorded in `infra/docker/README.md` and `RELEASE_STATUS.md`. No image built
# from this file has been pushed to any registry; see Known gap 68.

# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.11.7

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --------------------------------------------------------------------------------------------
# build — resolve and install the locked environment into /app/.venv
# --------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build

COPY --from=uv /uv /usr/local/bin/uv

# A C toolchain, in the build stage only. Not every locked dependency publishes a wheel for
# every architecture — `obspy` 1.5.1 has no manylinux aarch64 wheel and is built from its sdist
# here — and an image whose build depends on which wheels happen to exist for the builder's CPU
# is not a portable deployment unit (ADR-0014). The compiled artefacts land in /app/.venv; the
# toolchain does not reach the runtime stage.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install --no-install-recommends --yes build-essential \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Extra dependency groups to install, e.g. "--extra ml --extra surrogate" for the GPU jobs.
# Empty by default: the base image carries the runtime dependencies only.
ARG EXTRAS=""

# Dependencies first, without the project, so a source-only change does not re-download them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable ${EXTRAS}

# Then the package itself. --no-editable installs it into site-packages, so the runtime stage
# needs the virtualenv and nothing else from this stage.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable ${EXTRAS}

# --------------------------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="serac" \
      org.opencontainers.image.description="Open model of high-mountain rock-ice avalanche cascades" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/dizzy1900/serac"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Shared libraries the wheels expect from the system rather than vendoring. `libexpat1` is not
# in `python:3.12-slim-bookworm` and rasterio's bundled GDAL links it, so without this line
# `import rasterio` fails at run time and every `serac` sub-command dies on import — which is
# how it was found: the first image built from this file could not run `serac --version`.
# `libgomp1` is OpenMP, which lightgbm (the `ml` extra) loads.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install --no-install-recommends --yes libexpat1 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv

# The jobs bind-mount the DVC-managed repository at /work and write ledger entries into it, so
# the container must not run as root: a job that writes data/manifest.jsonl as uid 0 would leave
# a ledger the operator cannot append to.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin serac
WORKDIR /work
USER 10001

ENTRYPOINT ["serac"]
CMD ["--help"]
