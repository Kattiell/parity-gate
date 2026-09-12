"""parity-gate: prove an API rewrite did not break the contract.

The package is deliberately dependency-free (standard library only) so that a
reviewer can clone the repository and run the whole pipeline without a network
round trip or a virtualenv full of transitive packages.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
