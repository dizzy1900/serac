"""Every command module is reachable from `serac`.

`src/serac/cli_watch.py` held eleven commands and 460 lines and was never mounted on the app, so
M3's documented reproduction sequence could not be run as written by anybody following the
documentation. Nothing detected that, because a Typer sub-app that is never added is not an error
— it is just absent. This is the ratchet that makes it one.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
import typer
from typer.testing import CliRunner

import serac
from serac.cli import app

runner = CliRunner()

# Modules whose commands are deliberately mounted somewhere other than as a sub-app of their own
# name. Each entry names where to find them instead, so an exemption cannot be silent.
MOUNTED_ELSEWHERE: dict[str, str] = {
    "cli_seismic": "its commands are added to `serac ingest` (fdsn, comcat, hydro)",
    "cli_underwriting": "mounted as the top-level `serac underwriting-check`",
}


def _command_modules() -> list[str]:
    return sorted(
        m.name
        for m in pkgutil.iter_modules(serac.__path__)
        if m.name.startswith("cli_") and m.name != "cli"
    )


def _mounted_group_names() -> set[str]:
    return {group.name for group in app.registered_groups if group.name}


def _mounted_apps() -> set[int]:
    return {id(group.typer_instance) for group in app.registered_groups}


def test_there_are_command_modules_to_check() -> None:
    assert len(_command_modules()) > 5, "the discovery is doing nothing if it finds almost nothing"


@pytest.mark.parametrize("module_name", _command_modules())
def test_every_command_module_is_reachable_from_the_cli(module_name: str) -> None:
    module = importlib.import_module(f"serac.{module_name}")
    sub_app = getattr(module, "app", None)
    if not isinstance(sub_app, typer.Typer):
        pytest.skip(f"{module_name} exposes no Typer app of its own")
    if module_name in MOUNTED_ELSEWHERE:
        pytest.skip(f"{module_name}: {MOUNTED_ELSEWHERE[module_name]}")
    assert id(sub_app) in _mounted_apps(), (
        f"serac.{module_name} defines a Typer app that `serac` never mounts, so none of its "
        f"commands can be run. Add `app.add_typer({module_name}.app, name=...)` in "
        "src/serac/cli.py, or record it in MOUNTED_ELSEWHERE with where it lives instead."
    )


def test_the_watch_group_is_mounted_and_its_commands_are_listed() -> None:
    """M3's reproduction sequence, in the order `cli_watch`'s own docstring gives it."""
    assert "watch" in _mounted_group_names()
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    for command in (
        "select-track",
        "plan-network",
        "submit-insar",
        "poll-insar",
        "slope-units",
        "mintpy",
        "aggregate",
        "optical",
        "backtest",
        "tiers",
    ):
        assert command in result.stdout, f"`serac watch {command}` is not reachable"


def test_an_exemption_names_a_module_that_exists() -> None:
    known = set(_command_modules())
    unknown = sorted(set(MOUNTED_ELSEWHERE) - known)
    assert not unknown, f"MOUNTED_ELSEWHERE names modules that do not exist: {unknown}"
