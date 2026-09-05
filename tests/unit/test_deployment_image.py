"""The deployment unit: `infra/docker/Dockerfile` and the job manifests that reference it.

Known gap 68 was that three job manifests named `ghcr.io/dizzy1900/serac:0.1.0`, one of them
annotated `# built from infra/docker/Dockerfile`, while no `Dockerfile` existed anywhere in the
tree and no image had ever been pushed to that registry. An operator following the manifests
would have got `manifest unknown` from ghcr.io. Nothing checked either half, so nothing caught
it.

Three rules, each of which makes a class of that failure impossible rather than fixing the one
instance:

1. **A concrete image reference is a claim that an image exists, so it carries provenance.**
   Same rule the manifests already apply to core-hour figures (`basis:`) and to their own
   maturity (`metadata.status`): a claim about the world needs its evidence beside it. Until an
   image is pushed, the reference must be an unresolvable placeholder that no operator can
   mistake for something pullable.
2. **A repository path named in an `infra/` file exists.** `# built from
   infra/docker/Dockerfile` pointed at nothing.
3. **The image cannot be built from an unlocked dependency set**, and cannot COPY a path the
   build context excludes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
JOBS_DIR = ROOT / "infra" / "jobs"
MANIFESTS = sorted(JOBS_DIR.glob("*.yaml"))

# `<registry>/serac:<git-sha>` and friends: angle brackets make it unresolvable, so a copy-paste
# into `docker run` fails loudly instead of reaching for a tag nobody published.
PLACEHOLDER_IMAGE = re.compile(r"^<[^<>]+>/[\w./-]+:<[^<>]+>[\w.-]*$")

# The evidence a concrete reference has to carry, beside it in the same mapping.
PUBLICATION_KEYS = frozenset({"registry", "digest", "pushed_utc", "pushed_by"})


def _images(node: object, path: str = "") -> list[tuple[str, str, dict[str, object]]]:
    """Every `image:` value in the document, with its path and its containing mapping."""
    found: list[tuple[str, str, dict[str, object]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key == "image" and isinstance(value, str):
                found.append((here, value, node))
            found.extend(_images(value, here))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_images(value, f"{path}[{i}]"))
    return found


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_every_image_reference_is_a_placeholder_or_carries_publication_provenance(
    manifest: Path,
) -> None:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for where, image, container in _images(doc):
        if PLACEHOLDER_IMAGE.match(image):
            continue
        published = container.get("image_published")
        missing = (
            PUBLICATION_KEYS
            if not isinstance(published, dict)
            else PUBLICATION_KEYS - set(published)
        )
        if missing:
            offenders.append(f"{where} = {image!r} (missing image_published: {sorted(missing)})")
    assert not offenders, (
        f"{manifest.name}: a resolvable image reference asserts that the image exists, and "
        "nothing in this repository substantiates that. Either keep the unresolvable "
        "`<registry>/serac:<git-sha>` placeholder, or push the image and record `registry`, "
        f"`digest`, `pushed_utc` and `pushed_by` beside the reference. Offenders: {offenders}"
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.name)
def test_no_manifest_names_a_registry_host_while_no_image_is_published(manifest: Path) -> None:
    """Belt and braces on rule 1, against the raw text rather than the parsed document.

    A registry host in a comment (`# built from ghcr.io/...`) is as misleading as one in a
    field, and the parser never sees comments.
    """
    text = manifest.read_text(encoding="utf-8")
    hosts = re.findall(r"\b(?:ghcr\.io|docker\.io|quay\.io|[\w.-]+\.amazonaws\.com)/\S+", text)
    assert not hosts, (
        f"{manifest.name} names registry references {hosts}. No serac image has been pushed to "
        "any registry (RELEASE_STATUS.md Known gap 68); naming one tells an operator to pull "
        "something that does not exist."
    )


# `data/` paths in a manifest are the job's DVC outputs — directories a run creates, which by
# design do not exist on a fresh clone. Everything else named in an `infra/` file is a
# repository artefact and has to be there.
_REPO_PATH = re.compile(
    r"(?<![\w./-])((?:src|tests|infra|docs|reports|scripts|contracts|baselines)/[\w./-]*[\w/])"
)
_INFRA_FILES = sorted(
    p for p in (ROOT / "infra").rglob("*") if p.suffix in {".yaml", ".yml", ".md"}
)


@pytest.mark.parametrize("infra_file", _INFRA_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_repository_path_named_in_infra_exists(infra_file: Path) -> None:
    text = infra_file.read_text(encoding="utf-8")
    dangling = sorted(
        {
            token
            for token in _REPO_PATH.findall(text)
            if not any(c in token for c in "*<>")
            if not (ROOT / token).exists()
        }
    )
    assert not dangling, (
        f"{infra_file.relative_to(ROOT)} points at repository paths that do not exist: "
        f"{dangling}. An operator following the file would look for a file that is not there."
    )


def test_the_deployment_image_has_a_dockerfile() -> None:
    """ADR-0014 and CLAUDE.md non-negotiable 5 both name a plain Docker image as the
    deployment unit. Either the means to build it is in the tree or the claim is not true."""
    assert DOCKERFILE.is_file(), (
        "infra/docker/Dockerfile is missing. ADR-0014 makes a plain Docker image the "
        "deployment unit and infra/jobs/*.yaml reference it; without this file the repository "
        "has no deployment unit and RELEASE_STATUS.md must say so."
    )


def test_the_image_can_only_be_built_from_the_locked_dependency_set() -> None:
    """`uv sync --frozen` fails if `uv.lock` and `pyproject.toml` have drifted.

    Without `--frozen` a build would silently re-resolve, and the image would contain a
    dependency set no committed lockfile describes — unreproducible, and unprovenanced.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    body = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    syncs = [line for line in body if "uv sync" in line]
    assert syncs, "the Dockerfile installs nothing with `uv sync`"
    unfrozen = [line.strip() for line in syncs if "--frozen" not in line]
    assert not unfrozen, f"`uv sync` without --frozen would re-resolve at build time: {unfrozen}"
    loose = [
        line.strip() for line in body if re.search(r"\b(pip install|uv pip install|uv add)\b", line)
    ]
    assert not loose, f"dependencies installed outside the lockfile: {loose}"


def test_the_entrypoint_is_the_serac_cli() -> None:
    """The manifests' `command:` lists all start with `serac`, so the image's entrypoint has to
    be the CLI itself, not a shell."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["serac"]' in text, (
        "the image entrypoint must be the `serac` CLI: every infra/jobs command list starts "
        "with `serac` and would not run otherwise"
    )


def test_the_dockerfile_python_matches_the_repository_python() -> None:
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    match = re.search(r"^ARG PYTHON_VERSION=(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert match is not None, "the Dockerfile does not pin PYTHON_VERSION"
    assert match.group(1) == pinned, (
        f"the image builds on Python {match.group(1)} while the repository pins {pinned}; the "
        "deployment unit would not be running the interpreter the tests ran on"
    )


def test_the_build_context_includes_everything_the_dockerfile_copies() -> None:
    """`.dockerignore` excludes everything and re-includes by name, so a new COPY that nobody
    re-includes fails at build time on a machine with Docker — which is not this one."""
    ignore_lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "*" in ignore_lines, ".dockerignore no longer denies by default; this test assumes it"
    allowed = {line[1:].rstrip("/") for line in ignore_lines if line.startswith("!")}

    copied: list[str] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        # COPY <src>... <dest>
        copied.extend(stripped.split()[1:-1])

    assert copied, "the Dockerfile copies nothing from the build context"
    absent = [src for src in copied if not (ROOT / src.rstrip("/")).exists()]
    assert not absent, f"the Dockerfile copies paths not in the tree: {absent}"
    excluded = [src for src in copied if src.rstrip("/") not in allowed]
    assert not excluded, (
        f"the Dockerfile copies {excluded}, which .dockerignore excludes from the build "
        "context: the build would fail with 'file not found'"
    )
