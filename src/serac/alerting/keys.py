"""Ed25519 signing keys for CAP messages: generate, store, load, fingerprint.

Handling rules, enforced here rather than left to a runbook:

* **A private key is never printed.** No function in this module returns or logs private key
  bytes; `describe` reports the public fingerprint only. The CLI prints a path and a
  fingerprint and nothing else.
* **A private key is never committed.** `write_private_key` refuses a path git would track
  (`git check-ignore` is consulted when a checkout is available) and refuses a suffix other
  than `.pem`. `.gitignore` carries `*.pem` and `secrets/` so the default location is ignored
  by construction.
* **File mode is 0600** on write, and `load_private_key` warns through
  `PrivateKeyPermissionsError` when it finds a key readable by anyone else.
* **No passphrase support in v0.** A passphrase serac cannot prompt for is a passphrase stored
  next to the key, which is not a security control. The control is filesystem permissions plus
  an operator-managed secret store; that is stated rather than implied.

The public key is a separate file and *is* safe to publish: verification needs it.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from serac.errors import SeracError

SIGNING_KEY_ENV = "SERAC_CAP_SIGNING_KEY"
"""Environment variable naming the PEM file holding the CAP signing private key."""

PUBLIC_KEY_ENV = "SERAC_CAP_PUBLIC_KEY"
"""Environment variable naming the PEM file holding the matching public key."""

PRIVATE_KEY_MODE = 0o600
PUBLIC_KEY_MODE = 0o644
PEM_SUFFIX = ".pem"


class SigningKeyError(SeracError):
    """A signing key could not be generated, written or read."""


class PrivateKeyPermissionsError(SigningKeyError):
    """A private key on disk is readable by more than its owner."""


class PrivateKeyWouldBeCommittedError(SigningKeyError):
    """The requested private-key path is not ignored by git; serac refuses to write it."""


def generate_keypair() -> Ed25519PrivateKey:
    """A fresh Ed25519 private key. The public half is `key.public_key()`."""
    return Ed25519PrivateKey.generate()


def public_key_fingerprint(public_key: Ed25519PublicKey) -> str:
    """`sha256:<hex>` over the 32 raw public key bytes. Safe to print, log and publish."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _git_ignores(path: Path) -> bool | None:
    """True/False when git can answer, None when there is no checkout or no git binary."""
    directory = path.parent if path.parent.exists() else Path.cwd()
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", str(path)],
            cwd=directory,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def write_private_key(key: Ed25519PrivateKey, path: Path, *, allow_tracked: bool = False) -> Path:
    """Write `key` as an unencrypted PKCS#8 PEM at 0600, refusing anywhere git would track it.

    `allow_tracked` exists for tests running outside a checkout; it does not relax the mode.
    """
    if path.suffix != PEM_SUFFIX:
        raise SigningKeyError(f"{path}: a private key file must end in {PEM_SUFFIX}")
    if not allow_tracked and _git_ignores(path) is False:
        raise PrivateKeyWouldBeCommittedError(
            f"{path} is not ignored by git; refusing to write a private key where it could be "
            "committed. Put it under a gitignored directory (the repo ignores '*.pem' and "
            "'secrets/') or outside the checkout entirely."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Create with the final mode rather than widening then narrowing: a world-readable window,
    # however short, is a window.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_KEY_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    os.chmod(path, PRIVATE_KEY_MODE)
    return path


def write_public_key(public_key: Ed25519PublicKey, path: Path) -> Path:
    """Write the public half as a SubjectPublicKeyInfo PEM. Safe to commit and to publish."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    os.chmod(path, PUBLIC_KEY_MODE)
    return path


def load_private_key(path: Path, *, require_owner_only: bool = True) -> Ed25519PrivateKey:
    """Read a PKCS#8 Ed25519 private key. Refuses one others can read."""
    if not path.exists():
        raise SigningKeyError(
            f"{path}: no CAP signing key. Generate one with `serac alerting keygen` and point "
            f"{SIGNING_KEY_ENV} at it."
        )
    if require_owner_only:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PrivateKeyPermissionsError(
                f"{path}: mode {mode:04o} lets group or other read the private key; "
                f"run `chmod {PRIVATE_KEY_MODE:04o} {path}` before using it."
            )
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise SigningKeyError(f"{path}: not an Ed25519 private key ({type(loaded).__name__})")
    return loaded


def load_public_key(path: Path) -> Ed25519PublicKey:
    """Read a SubjectPublicKeyInfo Ed25519 public key."""
    if not path.exists():
        raise SigningKeyError(f"{path}: no CAP verification key")
    loaded = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(loaded, Ed25519PublicKey):
        raise SigningKeyError(f"{path}: not an Ed25519 public key ({type(loaded).__name__})")
    return loaded


@dataclass(frozen=True)
class KeyLocation:
    """Where the keys are, resolved from arguments then the environment. Values, never keys."""

    private_path: Path | None
    public_path: Path | None

    @property
    def can_sign(self) -> bool:
        return self.private_path is not None and self.private_path.exists()


def resolve_key_location(
    private_path: Path | None = None,
    public_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> KeyLocation:
    """Explicit paths win; otherwise read `SERAC_CAP_SIGNING_KEY` / `SERAC_CAP_PUBLIC_KEY`."""
    environment = os.environ if env is None else env
    private = private_path
    if private is None and environment.get(SIGNING_KEY_ENV):
        private = Path(environment[SIGNING_KEY_ENV])
    public = public_path
    if public is None and environment.get(PUBLIC_KEY_ENV):
        public = Path(environment[PUBLIC_KEY_ENV])
    if public is None and private is not None:
        candidate = private.with_suffix("").with_suffix(".pub.pem")
        public = candidate if candidate.exists() else None
    return KeyLocation(private_path=private, public_path=public)


def describe(public_key: Ed25519PublicKey) -> str:
    """One printable line about a key. Contains no private material by construction."""
    return f"Ed25519 {public_key_fingerprint(public_key)}"
