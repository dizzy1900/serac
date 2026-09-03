"""Shared pytest configuration.

Network is blocked for every test by `--disable-socket` in `pyproject.toml`. Tests that need it
carry the `online` marker: they are re-enabled only when `SERAC_ONLINE=1`, and they must skip
(not fail) when the network is unreachable. Tests marked `redis` need a live server.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

# torch and lightgbm each ship their own OpenMP runtime. On macOS arm64 loading both into
# one process segfaults, which surfaced as 17 intermittent failures that depended on how
# xdist happened to distribute the modules. Capping the thread count before either import
# makes the runtimes coexist. This has to be set here, in the root conftest, because pytest
# loads it before any test module: a directory-level conftest works only by luck of ordering.
for _threads_var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_threads_var, "1")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "data" / "fixtures"
SYNTHETIC_DIR = REPO_ROOT / "tests" / "fixtures" / "synthetic"


def _online_requested() -> bool:
    return os.environ.get("SERAC_ONLINE", "0") == "1"


def _redis_reachable(url: str) -> bool:
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=1.0)
        return bool(client.ping())
    except Exception:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    redis_url = os.environ.get("SERAC_REDIS_URL")
    redis_ok: bool | None = None
    for item in items:
        if "online" in item.keywords:
            if _online_requested():
                item.add_marker(pytest.mark.enable_socket)
            else:
                item.add_marker(pytest.mark.skip(reason="online test; set SERAC_ONLINE=1"))
        if "redis" in item.keywords:
            if redis_url is None:
                item.add_marker(pytest.mark.skip(reason="SERAC_REDIS_URL not set"))
                continue
            if redis_ok is None:
                redis_ok = _redis_reachable(redis_url)
            if redis_ok:
                item.add_marker(pytest.mark.enable_socket)
            else:
                item.add_marker(pytest.mark.skip(reason=f"Redis at {redis_url} unreachable"))


def require_network(host: str, port: int = 443, timeout: float = 3.0) -> None:
    """Skip the calling online test when `host:port` cannot be reached."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        pytest.skip(f"network unreachable for {host}:{port}: {exc}")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def synthetic_dir() -> Path:
    return SYNTHETIC_DIR


@pytest.fixture(autouse=True)
def _stable_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the width Click/rich wrap CLI output to.

    Help text and usage errors are wrapped to the terminal width, so an assertion on an
    option name passes on a wide developer terminal and fails on CI's 80 columns. Every test
    sees the same width.
    """
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERMINAL_WIDTH", "200")
