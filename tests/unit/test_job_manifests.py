"""`infra/jobs/` manifests and the README table that lists them.

Known gap 61 was that three Prompt 2 manifests existed on disk and none of them appeared in
the README's table, so an operator reading the documented list would not know they were there.
Nothing checked it, so nothing caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

JOBS_DIR = Path(__file__).resolve().parents[2] / "infra" / "jobs"
MANIFESTS = sorted(JOBS_DIR.glob("*.yaml"))


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
