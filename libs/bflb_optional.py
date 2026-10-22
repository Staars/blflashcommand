"""Load dependencies only when their optional feature is requested."""

from importlib import import_module

_PACKAGE_EXTRAS = {
    "pycryptodome": "aes",
    "ecdsa": "ecc",
    "pycryptoplus": "xts",
    "pylink-square": "jlink",
    "pycklink": "cklink",
    "telnetlib-313-and-up": "openocd",
}


def require_dependency(module, package, feature, extra=None):
    """Import an optional module, or explain how to enable the feature."""
    try:
        return import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module.split(".")[0]:
            raise
        raise ImportError(
            "{} requires {}; install it with: uv sync --extra {} "
            "or python -m pip install '{}'".format(
                feature, package, extra or _PACKAGE_EXTRAS.get(package, package), package
            )
        ) from exc
