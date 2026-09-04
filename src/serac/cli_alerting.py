"""`serac alerting`: keys, CAP generation, signature verification and dispatch.

Nothing in this module transmits anything unless the operator passes both `--sink http` and
`--send`, with an explicit `--endpoint`. The default sink writes files.

Key handling, in one place so an operator can follow it:

    serac alerting keygen --out secrets/cap-signing.pem
    export SERAC_CAP_SIGNING_KEY=secrets/cap-signing.pem
    export SERAC_CAP_PUBLIC_KEY=secrets/cap-signing.pub.pem

`keygen` refuses to write a private key anywhere git would track it, writes it 0600, and
prints only the path and the public fingerprint -- never the key. `.gitignore` carries `*.pem`
and `secrets/`. Distribute the **public** PEM to anyone who must verify serac's messages;
`serac alerting verify` takes it with `--public-key`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from serac.adapters.alerting.file_sink import FileAlertSink
from serac.adapters.alerting.http_sink import HttpAlertSink
from serac.alerting.example import check_forecast
from serac.alerting.generator import build_alert
from serac.alerting.keys import (
    PUBLIC_KEY_ENV,
    SIGNING_KEY_ENV,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_fingerprint,
    resolve_key_location,
    write_private_key,
    write_public_key,
)
from serac.alerting.signing import is_signed, signature_key_name, verify_cap_signature
from serac.domain.forecast import CascadeForecast
from serac.errors import SeracError
from serac.ports.alert_sink import AlertSink
from serac.validation.cap import CapValidator

app = typer.Typer(help="CAP 1.2 alerting: signing keys, message generation, sinks.")

REPO_OPTION = typer.Option(Path(), "--repo", help="Repository root.")
DEFAULT_KEY_PATH = Path("secrets") / "cap-signing.pem"


@app.command("keygen")
def keygen(
    out: Annotated[Path, typer.Option("--out", help="Private key PEM (gitignored).")] = (
        DEFAULT_KEY_PATH
    ),
    public_out: Annotated[
        Path | None, typer.Option("--public-out", help="Public key PEM; defaults beside --out.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing key.")] = False,
) -> None:
    """Generate an Ed25519 CAP signing keypair. The private key is never printed."""
    if out.exists() and not force:
        typer.secho(
            f"{out} exists; refusing to overwrite a signing key without --force",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    public_path = public_out or out.with_suffix("").with_suffix(".pub.pem")
    key = generate_keypair()
    try:
        write_private_key(key, out)
    except SeracError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    write_public_key(key.public_key(), public_path)
    typer.echo(f"private key : {out} (mode 0600, never printed, never committed)")
    typer.echo(f"public key  : {public_path} (safe to publish)")
    typer.echo(f"fingerprint : {public_key_fingerprint(key.public_key())}")
    typer.echo("")
    typer.echo(f"export {SIGNING_KEY_ENV}={out}")
    typer.echo(f"export {PUBLIC_KEY_ENV}={public_path}")


@app.command("fingerprint")
def fingerprint(
    public_key: Annotated[
        Path | None, typer.Option("--public-key", help=f"PEM; defaults to ${PUBLIC_KEY_ENV}.")
    ] = None,
) -> None:
    """Print the fingerprint of a public key."""
    location = resolve_key_location(public_path=public_key)
    if location.public_path is None:
        typer.secho(f"no public key: pass --public-key or set {PUBLIC_KEY_ENV}", fg="red")
        raise typer.Exit(2)
    typer.echo(public_key_fingerprint(load_public_key(location.public_path)))


@app.command("cap")
def cap(
    forecast_json: Annotated[
        Path | None,
        typer.Option("--forecast", help="A CascadeForecast JSON file; omit for the check fixture."),
    ] = None,
    repo: Path = REPO_OPTION,
    out_dir: Annotated[
        Path | None, typer.Option("--out-dir", help="Where the file sink writes.")
    ] = None,
    sign: Annotated[bool, typer.Option("--sign/--no-sign", help="Sign with the CAP key.")] = False,
    signing_key: Annotated[
        Path | None, typer.Option("--signing-key", help=f"PEM; defaults to ${SIGNING_KEY_ENV}.")
    ] = None,
    sink: Annotated[str, typer.Option("--sink", help="file | http")] = "file",
    endpoint: Annotated[
        str | None, typer.Option("--endpoint", help="Required for --sink http.")
    ] = None,
    send: Annotated[
        bool, typer.Option("--send", help="Actually transmit on the HTTP sink. Off by default.")
    ] = False,
) -> None:
    """Build a CAP 1.2 message from a forecast, validate it, and hand it to a sink."""
    forecast = _load_forecast(forecast_json)
    key = None
    if sign:
        location = resolve_key_location(private_path=signing_key)
        if location.private_path is None:
            typer.secho(
                f"--sign needs a key: pass --signing-key or set {SIGNING_KEY_ENV}", fg="red"
            )
            raise typer.Exit(2)
        key = load_private_key(location.private_path)
    validator = CapValidator(repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd")
    build = build_alert(forecast, sent=forecast.issued_utc, validator=validator, private_key=key)
    typer.echo(f"identifier : {build.message.identifier}")
    typer.echo(f"status     : {build.message.status}  ({build.status_rule})")
    typer.echo(f"severity   : {build.message.info[0].severity}  ({build.severity_rule})")
    typer.echo(f"urgency    : {build.message.info[0].urgency}  ({build.urgency_rule})")
    typer.echo(f"area       : {build.area_rule}")
    typer.echo(f"signed     : {build.signed}")

    target = _sink(sink, out_dir or (repo / "reports" / "e2e" / "cap"), endpoint, send)
    delivery = target.deliver(build.message)
    typer.echo(
        f"delivery   : sink={delivery.sink} delivered={delivery.delivered} {delivery.detail}"
    )
    if delivery.target:
        typer.echo(f"             -> {delivery.target}")


def _sink(kind: str, directory: Path, endpoint: str | None, send: bool) -> AlertSink:
    if kind == "file":
        return FileAlertSink(directory, log=True)
    if kind == "http":
        if not endpoint:
            typer.secho("--sink http requires --endpoint; there is no default", fg="red")
            raise typer.Exit(2)
        if send:
            typer.secho(
                f"--send is set: this will POST the message to {endpoint}", fg=typer.colors.YELLOW
            )
        return HttpAlertSink(endpoint, enabled=send)
    typer.secho(f"unknown sink {kind!r}; use 'file' or 'http'", fg="red")
    raise typer.Exit(2)


@app.command("verify")
def verify(
    xml_path: Annotated[Path, typer.Argument(help="A CAP XML file.")],
    public_key: Annotated[
        Path | None, typer.Option("--public-key", help=f"PEM; defaults to ${PUBLIC_KEY_ENV}.")
    ] = None,
    repo: Path = REPO_OPTION,
) -> None:
    """Verify a CAP message's enveloped Ed25519 signature and its XSD validity."""
    xml = xml_path.read_bytes()
    validator = CapValidator(repo / "contracts" / "vendor" / "cap" / "CAP-v1.2.xsd")
    problems = validator.errors(xml)
    typer.echo(f"xsd        : {problems if problems else 'valid'}")
    if not is_signed(xml):
        typer.secho("signature  : ABSENT — this message is unsigned", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.echo(f"key name   : {signature_key_name(xml)}")
    location = resolve_key_location(public_path=public_key)
    if location.public_path is None:
        typer.secho(
            f"signature  : cannot check — pass --public-key or set {PUBLIC_KEY_ENV}", fg="red"
        )
        raise typer.Exit(2)
    check = verify_cap_signature(xml, load_public_key(location.public_path))
    colour = typer.colors.GREEN if check.valid else typer.colors.RED
    typer.secho(f"signature  : {'VALID' if check.valid else 'INVALID'} — {check.reason}", fg=colour)
    if not check.valid or problems:
        raise typer.Exit(1)


def _load_forecast(path: Path | None) -> CascadeForecast:
    if path is None:
        typer.secho(
            "no --forecast given: using the FICTIONAL check forecast from "
            "serac.alerting.example. It describes no real place or event.",
            fg=typer.colors.YELLOW,
        )
        return check_forecast()
    return CascadeForecast.model_validate(json.loads(path.read_text(encoding="utf-8")))
