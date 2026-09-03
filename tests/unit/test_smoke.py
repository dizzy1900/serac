"""Bootstrap smoke tests: package imports, CLI runs, network guard is active."""

from __future__ import annotations

import socket

import pytest
from typer.testing import CliRunner

from serac import __version__
from serac.cli import app
from serac.settings import SeracSettings


def test_version_is_set() -> None:
    assert __version__


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "serac" in result.output


def test_settings_load_without_env() -> None:
    settings = SeracSettings(_env_file=None)
    assert settings.serac_online is False
    assert settings.earthdata_username is None


def test_network_is_blocked_in_unit_tests() -> None:
    with pytest.raises(Exception, match="socket"):
        socket.create_connection(("example.com", 80), timeout=1)
