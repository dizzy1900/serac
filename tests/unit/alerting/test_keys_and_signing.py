"""Key handling and the enveloped CAP signature.

The load-bearing assertions here are the negative ones: a private key must not be writable
where git would track it, must not be readable by anyone else, and a signature must fail when
the document changed, when the key is wrong, or when the algorithm is not the one we support.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from serac.alerting.keys import (
    PRIVATE_KEY_MODE,
    PUBLIC_KEY_ENV,
    SIGNING_KEY_ENV,
    PrivateKeyPermissionsError,
    PrivateKeyWouldBeCommittedError,
    SigningKeyError,
    describe,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_fingerprint,
    resolve_key_location,
    write_private_key,
    write_public_key,
)
from serac.alerting.signing import (
    ED25519_SIGNATURE,
    CapSignatureError,
    is_signed,
    sign_cap_xml,
    sign_message,
    signature_key_name,
    verify_cap_signature,
)
from serac.validation.cap import CapValidator


@pytest.fixture
def keypair_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    return directory


def test_private_key_is_written_0600_and_round_trips(keypair_dir: Path) -> None:
    key = generate_keypair()
    path = write_private_key(key, keypair_dir / "cap.pem", allow_tracked=True)
    assert stat.S_IMODE(path.stat().st_mode) == PRIVATE_KEY_MODE
    reloaded = load_private_key(path)
    assert public_key_fingerprint(reloaded.public_key()) == public_key_fingerprint(key.public_key())


def test_private_key_bytes_never_appear_in_a_printable_description(keypair_dir: Path) -> None:
    key = generate_keypair()
    path = write_private_key(key, keypair_dir / "cap.pem", allow_tracked=True)
    text = describe(key.public_key())
    assert "PRIVATE" not in text
    assert path.read_text(encoding="utf-8") not in text
    assert text.startswith("Ed25519 sha256:")


def test_write_refuses_a_non_pem_suffix(keypair_dir: Path) -> None:
    with pytest.raises(SigningKeyError, match=r"must end in \.pem"):
        write_private_key(generate_keypair(), keypair_dir / "cap.key", allow_tracked=True)


def test_write_refuses_a_path_git_would_track(tmp_path: Path) -> None:
    """The guard that stops a signing key reaching a commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"]):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "ignored").mkdir()

    with pytest.raises(PrivateKeyWouldBeCommittedError):
        write_private_key(generate_keypair(), repo / "tracked.pem")

    written = write_private_key(generate_keypair(), repo / "ignored" / "cap.pem")
    assert written.exists()


def test_load_refuses_a_world_readable_key(keypair_dir: Path) -> None:
    path = write_private_key(generate_keypair(), keypair_dir / "cap.pem", allow_tracked=True)
    path.chmod(0o644)
    with pytest.raises(PrivateKeyPermissionsError, match="group or other"):
        load_private_key(path)
    assert load_private_key(path, require_owner_only=False) is not None


def test_load_public_key_round_trips(keypair_dir: Path) -> None:
    key = generate_keypair()
    path = write_public_key(key.public_key(), keypair_dir / "cap.pub.pem")
    assert public_key_fingerprint(load_public_key(path)) == public_key_fingerprint(key.public_key())


def test_resolve_key_location_reads_the_environment(tmp_path: Path) -> None:
    env = {SIGNING_KEY_ENV: str(tmp_path / "a.pem"), PUBLIC_KEY_ENV: str(tmp_path / "a.pub.pem")}
    location = resolve_key_location(env=env)
    assert location.private_path == tmp_path / "a.pem"
    assert location.public_path == tmp_path / "a.pub.pem"
    assert location.can_sign is False  # nothing on disk


# -- signatures ---------------------------------------------------------------------------------


def _alert_xml() -> bytes:
    from serac.alerting.example import check_forecast
    from serac.alerting.generator import build_alert

    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, xsd_path=_xsd())
    assert build.message.xml
    return build.message.xml.encode("utf-8")


def _xsd() -> Path:
    return Path(__file__).resolve().parents[3] / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd"


def test_signed_message_still_validates_against_the_cap_xsd() -> None:
    key = generate_keypair()
    signed = sign_cap_xml(_alert_xml(), key)
    assert CapValidator(_xsd()).errors(signed) == []
    assert is_signed(signed)


def test_signature_verifies_and_names_the_key() -> None:
    key = generate_keypair()
    signed = sign_cap_xml(_alert_xml(), key)
    check = verify_cap_signature(signed, key.public_key())
    assert check.valid, check.reason
    assert check.signature_method == ED25519_SIGNATURE
    assert signature_key_name(signed) == f"ed25519:{public_key_fingerprint(key.public_key())}"


def test_a_different_key_does_not_verify() -> None:
    signed = sign_cap_xml(_alert_xml(), generate_keypair())
    check = verify_cap_signature(signed, generate_keypair().public_key())
    assert not check.valid
    assert "does not verify" in check.reason


def test_tampering_with_the_alert_breaks_the_digest() -> None:
    key = generate_keypair()
    signed = sign_cap_xml(_alert_xml(), key)
    tampered = signed.replace(b"fictional-check-aoi", b"somewhere-real--", 1)
    assert tampered != signed
    check = verify_cap_signature(tampered, key.public_key())
    assert not check.valid
    assert "changed after it was signed" in check.reason


def test_unsigned_document_reports_no_signature() -> None:
    check = verify_cap_signature(_alert_xml(), generate_keypair().public_key())
    assert not check.valid
    assert check.reason == "no ds:Signature element"
    assert signature_key_name(_alert_xml()) is None


def test_refuses_to_sign_twice() -> None:
    key = generate_keypair()
    signed = sign_cap_xml(_alert_xml(), key)
    with pytest.raises(CapSignatureError, match="already carries"):
        sign_cap_xml(signed, key)


def test_refuses_a_non_cap_root() -> None:
    with pytest.raises(CapSignatureError, match=r"expected a CAP 1\.2 alert"):
        sign_cap_xml(b"<other/>", generate_keypair())


def test_sign_message_needs_rendered_xml() -> None:
    from serac.alerting.example import check_forecast
    from serac.alerting.generator import build_alert

    forecast = check_forecast()
    build = build_alert(forecast, sent=forecast.issued_utc, xsd_path=_xsd())
    stripped = build.message.model_copy(update={"xml": None})
    with pytest.raises(CapSignatureError, match="no rendered XML"):
        sign_message(stripped, generate_keypair())
    signed = sign_message(build.message, generate_keypair())
    assert signed.xml and is_signed(signed.xml)
