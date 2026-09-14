"""A catalogue API served twice: ``/legacy`` as it was, ``/next`` as rewritten.

The rewrite contains defects on purpose, and they are the defects that actually
survive code review in a migration, not obvious ones:

* ``price`` is serialised as a string by the new serialiser.
* ``stock`` was dropped because "the front end reads availability now".
* ``discount`` can come back ``null`` for one product.
* ``warehouseId`` and ``internalCost`` are new: one harmless, one an
  information leak nobody asked for.
* ``total`` disagrees with the number of items returned: the pagination rewrite
  counts differently.
* The items come back in a different order.
* A missing product answers ``200 {"data": null}`` instead of ``404``, because
  the new handler is "tolerant".
* ``/inventory/sync-status`` fails intermittently, so any diff taken from it is
  worthless until that is fixed.

Nothing here is random: the intermittent failure is keyed to a request counter,
so the demo and CI produce the same findings on every run.

Bound to 127.0.0.1 only. It serves fixtures and holds no state worth reaching.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

LEGACY = "legacy"
NEXT = "next"

PRODUCTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "title": "Torx screwdriver T20",
        "category": "tools",
        "price": 19.9,
        "stock": 12,
        "discount": 0.1,
        "tags": ["hand-tool"],
    },
    {
        "id": 2,
        "title": "Impact drill 750W",
        "category": "tools",
        "price": 459.0,
        "stock": 3,
        "discount": 0.0,
        "tags": ["power-tool", "corded"],
    },
    {
        "id": 3,
        "title": "Safety goggles",
        "category": "ppe",
        "price": 39.5,
        "stock": 80,
        "discount": 0.05,
        "tags": ["ppe"],
    },
    {
        "id": 4,
        "title": "Work gloves size L",
        "category": "ppe",
        "price": 24.0,
        "stock": 0,
        "discount": 0.0,
        "tags": ["ppe", "consumable"],
    },
]

INTERNAL_COST = {1: 8.4, 2: 291.0, 3: 14.2, 4: 9.6}
WAREHOUSE = {1: 7, 2: 7, 3: 12, 4: 12}


class MockServer(ThreadingHTTPServer):
    """Threaded server that keeps one call counter per (variant, endpoint)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.counters: defaultdict[str, int] = defaultdict(int)
        self.lock = threading.Lock()
        #: TCP connections accepted. With keep-alive this stays far below the
        #: number of requests, which is what makes connection reuse testable
        #: rather than merely claimed.
        self.connections = 0

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        with self.lock:
            self.connections += 1
        super().process_request(request, client_address)

    def tick(self, key: str) -> int:
        with self.lock:
            self.counters[key] += 1
            return self.counters[key]

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        if isinstance(host, bytes | bytearray):
            host = host.decode()
        return f"http://{host}:{port}"


def _legacy_product(product: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "category", "price", "stock", "discount", "tags")
    return {key: product[key] for key in keys}


def _next_product(product: dict[str, Any]) -> dict[str, Any]:
    """The rewritten serialiser, defects included."""
    return {
        "id": product["id"],
        "title": product["title"],
        "category": product["category"],
        # Defect: money serialised as a string.
        "price": f"{product['price']:.2f}",
        # Defect: `stock` dropped.
        # Defect: discount is null instead of 0.0 for one product.
        "discount": None if product["id"] == 4 else product["discount"],
        "tags": product["tags"],
        # Additive, harmless.
        "warehouseId": WAREHOUSE[product["id"]],
        # Additive and a data-exposure regression.
        "internalCost": INTERNAL_COST[product["id"]],
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "parity-gate-mock"

    # Keep test output readable; the harness records what it needs itself.
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        """Only /graphql, which is the whole point: one URL, one method, and
        the operation type hidden inside the body."""
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) != 2 or segments[0] not in {LEGACY, NEXT} or segments[1] != "graphql":
            return self._send(404, {"error": "no such endpoint", "path": parts.path})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            document = json.loads(self.rfile.read(length) or b"{}").get("query", "")
        except json.JSONDecodeError:
            return self._send(400, {"errors": [{"message": "malformed request body"}]})

        if "createOrder" in document:
            return self._send(200, {"data": {"createOrder": {"id": 99, "status": "placed"}}})

        products = [_legacy_product(p) for p in PRODUCTS[:2]]
        if segments[0] == LEGACY:
            return self._send(200, {"data": {"products": products}})

        # The rewrite, with the same defects the REST side has, plus the one
        # that only GraphQL can have: a 200 carrying partial data and an error.
        return self._send(
            200,
            {
                "data": {"products": [_next_product(p) for p in PRODUCTS[:2]]},
                "errors": [{"message": "Cannot resolve field 'stock' on type 'Product'"}],
            },
        )

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        if not segments or segments[0] not in {LEGACY, NEXT}:
            return self._send(404, {"error": "unknown variant", "expected": [LEGACY, NEXT]})

        variant, rest = segments[0], segments[1:]
        query = parse_qs(parts.query)

        if rest == ["health"]:
            return self._health(variant)
        if rest == ["whoami"]:
            return self._whoami()
        if rest == ["inventory", "sync-status"]:
            return self._sync_status(variant)
        if rest == ["products"]:
            return self._product_list(variant, query)
        if len(rest) == 2 and rest[0] == "products":
            return self._product_detail(variant, rest[1])
        return self._send(404, {"error": "no such endpoint", "path": parts.path})

    # -- endpoints ----------------------------------------------------------

    def _product_list(self, variant: str, query: dict[str, list[str]]) -> None:
        limit = _int(query.get("limit", ["4"])[0], default=4)
        selected = PRODUCTS[:limit]
        server: MockServer = self.server  # type: ignore[assignment]
        sequence = server.tick(f"{variant}:products")

        if variant == LEGACY:
            body = {
                "products": [_legacy_product(p) for p in selected],
                "total": len(selected),
                "skip": 0,
                "limit": limit,
                "meta": {"requestId": f"legacy-{sequence:06d}", "generatedAt": _stamp(sequence)},
            }
        else:
            body = {
                # Defect: the rewrite returns the page in a different order.
                "products": [_next_product(p) for p in reversed(selected)],
                # Defect: the new counter is off by one against what it returned.
                "total": max(len(selected) - 1, 0),
                "skip": 0,
                "limit": limit,
                "meta": {"requestId": f"next-{sequence:06d}", "generatedAt": _stamp(sequence)},
            }
        self._send(200, body)

    def _product_detail(self, variant: str, raw_id: str) -> None:
        product_id = _int(raw_id, default=-1)
        product = next((p for p in PRODUCTS if p["id"] == product_id), None)

        if product is not None:
            # This endpoint was not part of the migration: both sides agree.
            return self._send(200, _legacy_product(product))

        if variant == LEGACY:
            return self._send(404, {"error": "product not found", "code": "PRODUCT_NOT_FOUND"})
        # Defect: the "tolerant" handler turns a missing record into a success.
        self._send(200, {"data": None})

    def _whoami(self) -> None:
        """Echo the credential back, so a test can prove two things at once:
        that the header actually reached the server, and that it does not
        survive into the evidence file afterwards."""
        self._send(200, {"authorization": self.headers.get("Authorization")})

    def _health(self, variant: str) -> None:
        server: MockServer = self.server  # type: ignore[assignment]
        sequence = server.tick(f"{variant}:health")
        # Both sides move on every call: a body that is noisy, not broken.
        self._send(200, {"status": "ok", "uptimeSeconds": 1000 + sequence})

    def _sync_status(self, variant: str) -> None:
        server: MockServer = self.server  # type: ignore[assignment]
        sequence = server.tick(f"{variant}:sync")
        if variant == NEXT and sequence % 3 == 2:
            # Defect: intermittent upstream failure, deterministic here so the
            # demo always shows the same triage.
            return self._send(503, {"error": "upstream busy", "retryAfter": 5})
        self._send(200, {"status": "idle", "pending": 0})

    # -- plumbing -----------------------------------------------------------

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _stamp(sequence: int) -> str:
    """A monotonically moving timestamp that owes nothing to the wall clock."""
    minutes, seconds = divmod(sequence * 7, 60)
    return f"2026-01-01T00:{minutes % 60:02d}:{seconds:02d}Z"


def start(port: int = 0) -> MockServer:
    """Start the mock on 127.0.0.1 in a daemon thread and return the server."""
    server = MockServer(("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="parity-gate-mock")
    thread.start()
    return server


def serve_forever(port: int = 8799) -> None:
    """Blocking entry point: ``python -m parity_gate.mock.server``."""
    server = MockServer(("127.0.0.1", port))
    print(f"mock api listening on {server.base_url}")
    print(f"  baseline  -> {server.base_url}/legacy")
    print(f"  candidate -> {server.base_url}/next")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    serve_forever()
