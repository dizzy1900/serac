# ADR-0001: Python 3.12, uv, ruff, mypy --strict, pytest-xdist

Date: 2026-09-03

## Status

Accepted

## Context

serac is a greenfield standalone project. The founding brief fixes the toolchain; this ADR
records it so it is not relitigated. The dev machine and CI both have `uv` and CPython 3.12.

## Decision

- Python `>=3.12,<3.13`, pinned in `.python-version`; the interpreter is managed by `uv`.
- `uv` owns the environment and the lockfile (`uv.lock`, committed). CI runs
  `uv sync --frozen --all-extras`.
- `ruff` does both linting (`E, F, W, I, B, UP, SIM, RUF, N, T20`; `T20` allowed in `cli*.py`
  and `tests/`) and formatting (double quotes, line length 100).
- `mypy --strict` on `src/` with the pydantic plugin, `warn_unused_ignores` and
  `warn_unreachable`. `ignore_missing_imports` is granted only per-module for untyped
  provider SDKs (obspy, asf_search, hyp3_sdk, cdsapi, rasterio, rioxarray, geopandas, pystac,
  zarr, pyproj, dvc, fakeredis); the list lives in `pyproject.toml`.
- `pytest` with `pytest-xdist` (`-n auto`) for the offline suite; `--strict-markers`;
  `--import-mode=importlib`.

## Consequences

- One command (`make lint typecheck test`) reproduces CI locally.
- Every `# type: ignore` must be necessary (`warn_unused_ignores`), which keeps the strict
  boundary honest at adapter edges.
- `make smoke-online` runs without xdist (`-p no:xdist`) so network skips are reported
  legibly.
