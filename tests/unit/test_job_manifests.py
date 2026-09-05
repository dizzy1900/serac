"""`infra/jobs/` manifests: the README table, the cost bases, and the argv they run.

Known gap 61 was that three Prompt 2 manifests existed on disk and none of them appeared in
the README's table, so an operator reading the documented list would not know they were there.
Nothing checked it, so nothing caught it.

The same shape of failure reached the commands themselves. `hyp3-insar-batch.yaml` and
`s1-stack.yaml` were written before the ingest adapters landed and told an operator to run
`serac ingest hyp3 --watch --confirm-bytes N` and `serac ingest s1 --product GRD
--confirm-bytes N`. The CLI has never had `--watch`, `--product` or `--confirm-bytes`; both
jobs would have died on `No such option` after the image pulled, and their `aws:` sketches
repeated the same argv a second time. `runout-ensemble-10k.yaml` passed `--workers
"$(NPROC)"`, which the exec-form `ENTRYPOINT ["serac"]` never expands. A `# NOTE: the CLI
flags below are PROPOSED` comment carried the risk instead of a check, and the note outlived
the adapters it was waiting for.

So the manifests are executable documents, not prose, and the rules below treat them that way:

1. **Every `command:` in a manifest resolves against the installed `serac` CLI** -- the
   sub-command exists, every flag exists, and every value the manifest's own `parameters:`
   supply for it parses. Nothing here runs a command; resolution stops at argument parsing.
2. **The `aws:` sketch is the generic `command`, not a second copy of it.** `${param}` becomes
   `Ref::param` and nothing else changes, so the annotation cannot drift from the job it
   annotates -- which is exactly how both halves of the wrong argv came to be written twice.
3. **The only substitution in an argv is a declared parameter.** The entrypoint is exec-form,
   so no shell expands `$(...)` or `$VAR`: such a token reaches the CLI verbatim.
4. **An ingest job executes.** `serac ingest` needs exactly one of `--dry-run` and `--yes`
   (`run()` in `src/serac/cli_ingest.py` exits 2 otherwise), so a manifest carrying neither
   describes a job that would exit non-zero, and one carrying `--dry-run` describes a
   scaled run that downloads nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import typer.main
import yaml

from serac.cli import app as serac_app

JOBS_DIR = Path(__file__).resolve().parents[2] / "infra" / "jobs"
MANIFESTS = sorted(JOBS_DIR.glob("*.yaml"))

CLI = typer.main.get_command(serac_app)
"""The real click command tree behind `serac`, the same object the entrypoint runs."""

AWS_SKETCH = "aws.batch_job_definition.containerProperties.command"
PARAMETER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
REF_PREFIX = "Ref::"


def test_there_are_manifests_to_check() -> None:
    assert MANIFESTS, f"no job manifests under {JOBS_DIR}"


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_manifest_is_listed_in_the_readme(manifest: Path) -> None:
    readme = (JOBS_DIR / "README.md").read_text(encoding="utf-8")
    assert f"`{manifest.name}`" in readme, (
        f"{manifest.name} is not in infra/jobs/README.md's manifest table: an operator reading "
        "the documented list would not know it exists"
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_manifest_parses_and_names_itself(manifest: Path) -> None:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{manifest.name} is not a YAML mapping"
    name = doc.get("metadata", {}).get("name")
    assert name == manifest.stem, f"{manifest.name} calls itself {name!r}"


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_cost_estimate_states_its_basis(manifest: Path) -> None:
    """A core-hour or GPU-hour figure with no stated basis is a guess wearing a number.

    Only estimates are policed, not resource requests: `resources.storage_gb` is what the job
    asks the host for, while `estimated_storage_gb` is a claim about what it will consume.
    """
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    cost_keys = ("core_hours", "gpu_hours", "cpu_fallback_hours")

    def _walk(node: object, path: str = "") -> list[str]:
        unsupported: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                is_cost = key.startswith("estimated_") or any(c in key for c in cost_keys)
                if is_cost:
                    # The basis sits beside the figure (`gpu_hours` / `basis`) or inside it
                    # (`estimated_core_hours: {low, high, basis}`).
                    beside = any("basis" in k for k in node)
                    inside = isinstance(value, dict) and any("basis" in k for k in value)
                    if not (beside or inside):
                        unsupported.append(here)
                unsupported.extend(_walk(value, here))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                unsupported.extend(_walk(value, f"{path}[{i}]"))
        return unsupported

    offenders = _walk(doc)
    assert not offenders, f"{manifest.name}: cost figures with no stated basis at {offenders}"


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_no_manifest_claims_to_have_been_executed(manifest: Path) -> None:
    """None has ever run, and `infra/jobs/README.md` says so. Keep the two in step."""
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    status = doc.get("metadata", {}).get("status", "designed")
    assert status == "designed", (
        f"{manifest.name} claims status {status!r}; README.md states that no manifest has been "
        "executed. Record an `observed:` block and update the README before changing this."
    )


# -- rules 1-4: the argv a manifest tells an operator to run ------------------------------------


def _parameters(doc: dict[str, Any]) -> dict[str, str]:
    """The manifest's declared job inputs, from either manifest shape."""
    for holder in (doc, doc.get("spec") or {}):
        declared = holder.get("parameters") if isinstance(holder, dict) else None
        if isinstance(declared, dict):
            return {str(k): str(v) for k, v in declared.items()}
    return {}


def _command_lists(node: object, path: str = "") -> list[tuple[str, list[str]]]:
    """Every `command`/`*_command` argv in the document, with its path.

    Both manifest shapes are covered (`command:` at the top level, `spec.command:`), and so is
    the `aws:` sketch, which is an argv like any other.
    """
    found: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) == "command" or str(key).endswith("_command"):
                assert isinstance(value, list) and all(isinstance(v, str) for v in value), (
                    f"{here} is not an argv list of strings: {value!r}"
                )
                found.append((here, [str(v) for v in value]))
            found.extend(_command_lists(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_command_lists(value, f"{path}[{index}]"))
    return found


def _substitute(argv: list[str], parameters: dict[str, str]) -> tuple[list[str], list[str]]:
    """Resolve `${param}` and `Ref::param` against the manifest's own declared defaults.

    Rule 3 lives here: anything else that looks like a substitution is a token the exec-form
    entrypoint hands to the CLI verbatim.
    """
    resolved: list[str] = []
    problems: list[str] = []
    for token in argv:
        if token.startswith(REF_PREFIX):
            name = token[len(REF_PREFIX) :]
            if name not in parameters:
                problems.append(f"{token!r} names no declared parameter")
                continue
            resolved.append(parameters[name])
            continue
        missing = [name for name in PARAMETER.findall(token) if name not in parameters]
        if missing:
            problems.append(f"{token!r} references undeclared parameters {missing}")
            continue
        expanded = PARAMETER.sub(lambda m: parameters[m.group(1)], token)
        if "$" in expanded:
            problems.append(
                f"{token!r} carries a substitution nothing expands: the image entrypoint is "
                'exec-form (ENTRYPOINT ["serac"]), so no shell runs and this token reaches '
                "the CLI as a literal argument. Write the value, or declare a parameter."
            )
            continue
        resolved.append(expanded)
    return resolved, problems


def resolution_error(argv: list[str]) -> str | None:
    """`None` when `argv` would parse as a `serac` invocation, else why it would not.

    Walks the sub-command tree and parses the leaf's arguments. `make_context` parses and
    validates; it does not invoke the command, so nothing here fetches, writes or trains.
    """
    if not argv or argv[0] != "serac":
        return f"argv does not start with the image entrypoint `serac`: {argv}"
    command: Any = CLI
    walked = ["serac"]
    args = list(argv[1:])
    while args and hasattr(command, "commands"):
        name = args[0]
        sub = command.commands.get(name)
        if sub is None:
            return (
                f"`{' '.join(walked)}` has no sub-command {name!r}; it has "
                f"{sorted(command.commands)}"
            )
        command, walked, args = sub, [*walked, name], args[1:]
    if hasattr(command, "commands"):
        return f"`{' '.join(walked)}` is a command group, not a runnable command"
    try:
        command.make_context(" ".join(walked), list(args))
    except Exception as exc:  # any parse failure is a job that dies on start
        return f"`{' '.join(walked)}` rejects {args}: {type(exc).__name__}: {exc}"
    return None


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_command_in_a_manifest_resolves_against_the_cli(manifest: Path) -> None:
    """Rules 1 and 3. A manifest is the operator's instruction sheet; a flag the CLI does not
    have is a job that fails after the image is pulled and the credentials are mounted."""
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    parameters = _parameters(doc)
    commands = _command_lists(doc)
    assert commands, f"{manifest.name} describes a job with no command to run"
    offenders: list[str] = []
    for where, argv in commands:
        resolved, problems = _substitute(argv, parameters)
        offenders.extend(f"{where}: {problem}" for problem in problems)
        if problems:
            continue
        error = resolution_error(resolved)
        if error is not None:
            offenders.append(f"{where}: {error}")
    assert not offenders, (
        f"{manifest.name} tells an operator to run a command the `serac` CLI would reject. "
        f"Reconcile the manifest with `serac --help` (never the other way round): {offenders}"
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_the_aws_sketch_runs_the_same_argv_as_the_job(manifest: Path) -> None:
    """Rule 2: one argv written twice is one argv that can be fixed once and stay wrong once."""
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    by_path = dict(_command_lists(doc))
    sketch = by_path.get(AWS_SKETCH)
    if sketch is None:
        return  # not every manifest carries a Batch job-definition sketch
    generic = by_path.get("command")
    assert generic is not None, (
        f"{manifest.name} has an {AWS_SKETCH} but no top-level `command:` for it to mirror"
    )
    expected = [
        f"{REF_PREFIX}{match.group(1)}" if (match := PARAMETER.fullmatch(token)) else token
        for token in generic
    ]
    assert sketch == expected, (
        f"{manifest.name}: the aws: block is an annotation of this job, not a second job. It "
        f"must be the generic command with `${{param}}` written as `Ref::param`.\n"
        f"  expected: {expected}\n  found:    {sketch}"
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_ingest_command_executes_rather_than_only_planning(manifest: Path) -> None:
    """Rule 4. `run()` in `src/serac/cli_ingest.py` exits 2 unless exactly one of `--dry-run`
    and `--yes` is present; `--poll` and `--receive` return before it and take neither."""
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for where, argv in _command_lists(doc):
        if argv[:2] != ["serac", "ingest"]:
            continue
        flags = {token for token in argv if token.startswith("--")}
        if flags & {"--poll", "--receive"}:
            assert not flags & {"--dry-run", "--yes"}, (
                f"{manifest.name} {where}: --poll/--receive take neither --dry-run nor --yes"
            )
            continue
        chosen = flags & {"--dry-run", "--yes"}
        if len(chosen) != 1:
            offenders.append(f"{where}: {sorted(chosen) or 'neither'}")
    assert not offenders, (
        f"{manifest.name}: an ingest job needs exactly one of --dry-run and --yes. With "
        "neither it exits 2 having done nothing; with --dry-run it is not a scaled run at "
        f"all, it prints a plan and writes not even a ledger line. Offenders: {offenders}"
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_no_manifest_hedges_that_its_flags_are_unreconciled(manifest: Path) -> None:
    """Both EO manifests opened with `# NOTE: the CLI flags below are PROPOSED; reconcile with
    `serac ingest` when the adapters land.` The adapters landed and the note stayed. Rule 1
    now decides the question on every run, so the hedge can only be wrong or redundant."""
    text = manifest.read_text(encoding="utf-8")
    hedges = re.findall(r"^.*\bPROPOSED\b.*$", text, re.M)
    assert not hedges, (
        f"{manifest.name} says its command is unreconciled with the CLI, while "
        "test_every_command_in_a_manifest_resolves_against_the_cli checks that it is "
        f"reconciled. Delete the note or fix the command: {hedges}"
    )


def test_the_resolver_would_catch_the_flags_that_were_wrong() -> None:
    """A checker that never fails checks nothing. These are the exact argv that shipped."""
    assert resolution_error(["serac", "ingest", "hyp3", "--aoi", "a", "--watch"]) is not None
    assert resolution_error(["serac", "ingest", "s1", "--aoi", "a", "--product", "GRD"]) is not None
    assert (
        resolution_error(["serac", "ingest", "s1", "--aoi", "a", "--confirm-bytes", "0"])
        is not None
    )
    # A required option the manifest forgot, and a value of the wrong type.
    assert resolution_error(["serac", "ingest", "s1", "--aoi", "a"]) is not None
    assert resolution_error(["serac", "runout", "run", "--workers", "$(NPROC)"]) is not None
    assert resolution_error(["serac", "not-a-command"]) is not None
    assert resolution_error(["python", "-m", "serac"]) is not None
    # ... and passes the argv that is actually right.
    assert (
        resolution_error(
            ["serac", "ingest", "s1", "--aoi", "a", "--from", "2026-06-01", "--to", "2026-08-31"]
        )
        is None
    )


def test_the_parameter_substitution_is_the_only_one_the_entrypoint_can_do() -> None:
    """Rule 3's own check: `$(NPROC)` is not a parameter and no shell is there to expand it."""
    resolved, problems = _substitute(["--workers", "${workers}"], {"workers": "8"})
    assert (resolved, problems) == (["--workers", "8"], [])
    _, shell = _substitute(["--workers", "$(NPROC)"], {"workers": "8"})
    assert shell and "exec-form" in shell[0]
    _, undeclared = _substitute(["--aoi", "${nope}"], {"workers": "8"})
    assert undeclared and "undeclared" in undeclared[0]
