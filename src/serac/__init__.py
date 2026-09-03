"""serac: open model of high-mountain rock-ice avalanche cascades."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("serac")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
