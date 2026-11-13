"""labctl package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("labctl")
except PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "unknown"
