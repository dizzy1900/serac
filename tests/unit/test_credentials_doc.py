"""Every key in `.env.example` is documented in `docs/CREDENTIALS.md`."""

from __future__ import annotations

from pathlib import Path


def _env_example_keys(text: str) -> list[str]:
    keys: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0])
    return keys


def test_every_env_example_key_is_in_credentials(repo_root: Path) -> None:
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    credentials = (repo_root / "docs" / "CREDENTIALS.md").read_text(encoding="utf-8")
    missing = [key for key in _env_example_keys(example) if f"`{key}`" not in credentials]
    assert not missing, f"docs/CREDENTIALS.md does not name these .env.example keys: {missing}"
