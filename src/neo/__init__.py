"""neo package."""

__all__ = ["__version__"]

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("neo-harnesster")
    except PackageNotFoundError:
        __version__ = "0.0.0"
except Exception:
    __version__ = "0.0.0"
