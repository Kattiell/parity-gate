"""A two-headed mock API used by the demo and by the integration tests."""

from parity_gate.mock.server import MockServer, serve_forever, start

__all__ = ["MockServer", "serve_forever", "start"]
