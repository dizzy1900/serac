# ADR-0010: offline test guard (pytest-socket) and the `online` / `redis` markers

Date: 2026-09-03

## Status

Accepted

## Context

Non-negotiable 4: all tests pass with no network on committed fixtures; network behaviour is
exercised by `make smoke-online`, which may skip. Convention alone does not stop a unit test
from quietly calling a provider.

## Decision

- `pyproject.toml` passes `--disable-socket --allow-unix-socket` to `pytest-socket` in
  `addopts`, so any socket open in the default suite raises and fails the test.
- Markers (`--strict-markers`): `online` (needs internet), `redis` (needs a live server),
  `slow`.
- `tests/conftest.py` re-enables sockets for `online` tests only when `SERAC_ONLINE=1`,
  otherwise skips them; `redis` tests run only when `SERAC_REDIS_URL` is set and the server
  answers `PING`. `online` tests must call `require_network(host)` and skip when the host is
  unreachable.
- `make test` = `pytest -n auto -m "not online and not redis"`;
  `make smoke-online` = `SERAC_ONLINE=1 pytest -m "online or redis" -p no:xdist -ra`.

## Consequences

- A test that needs the network fails mechanically unless it is marked, which is exactly
  the review signal `qa-reviewer` looks for.
- `make smoke-online` skipping is a valid outcome; it never turns CI red.
