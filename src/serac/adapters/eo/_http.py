"""HTTP plumbing shared by the EO adapters.

Adapters receive an `HttpClient` (a `Protocol`) so tests can inject a fake that serves
committed bytes; the production implementation wraps `httpx` and streams downloads to disk
while hashing, so the sha256 recorded in the ledger is the sha256 of the bytes on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

import httpx

from serac import __version__

DEFAULT_TIMEOUT_S = 120.0
CHUNK_BYTES = 1 << 20


class HttpClient(Protocol):
    """The minimal surface the adapters need. Keep it small so fakes stay trivial."""

    def stream_to(self, url: str, dest: Path) -> tuple[str, int]:
        """Download `url` to `dest`; return (sha256 hex, size in bytes)."""
        ...

    def head_content_length(self, url: str) -> int | None:
        """Content-Length of `url`, or None when the server does not say."""
        ...

    def get_json(self, url: str) -> Any:
        """GET `url` and decode the JSON body."""
        ...


def make_httpx_client(timeout_s: float = DEFAULT_TIMEOUT_S) -> httpx.Client:
    """An `httpx.Client` with redirects on and a serac user agent."""
    return httpx.Client(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"User-Agent": f"serac/{__version__} (+https://github.com/dizzy1900/serac)"},
    )


def sha256_and_size(path: Path) -> tuple[str, int]:
    """sha256 hex digest and byte size of a file (the values the ledger records)."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def download_to_path(client: httpx.Client, url: str, dest: Path) -> tuple[str, int]:
    """Stream `url` into `dest` atomically (via `dest.part`), hashing as it goes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with client.stream("GET", url) as response, part.open("wb") as fh:
            response.raise_for_status()
            for chunk in response.iter_bytes(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                fh.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size


class HttpxClient:
    """`HttpClient` backed by `httpx`; the production choice."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_httpx_client()

    def stream_to(self, url: str, dest: Path) -> tuple[str, int]:
        return download_to_path(self._client, url, dest)

    def head_content_length(self, url: str) -> int | None:
        response = self._client.head(url)
        response.raise_for_status()
        raw = response.headers.get("content-length")
        return int(raw) if raw is not None and raw.isdigit() else None

    def get_json(self, url: str) -> Any:
        response = self._client.get(url)
        response.raise_for_status()
        return response.json()
