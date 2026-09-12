"""Demo suites shipped inside the package.

They live here rather than in the repository's ``suites/`` directory for one
reason: ``pip install parity-gate`` followed by ``parity-gate demo`` has to work
from any directory. Resolving them from ``__file__`` relative to a source tree
worked only in an editable install from a clone, which is the narrowest possible
definition of "it works".
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

MODES = {
    "differential": "demo-catalog.toml",
    "contract": "demo-contract-guard.toml",
    "stability": "demo-catalog.toml",
}


def suite_path(mode: str = "differential") -> Path:
    """Filesystem path to a bundled demo suite.

    ``as_file`` is used rather than a bare ``__file__`` join so this keeps
    working if the package is ever loaded from a zip or a wheel-mounted import.
    """
    try:
        name = MODES[mode]
    except KeyError:
        raise ValueError(f"unknown demo mode {mode!r}; expected one of {sorted(MODES)}") from None

    with resources.as_file(resources.files(__package__).joinpath(name)) as path:
        return Path(path)
