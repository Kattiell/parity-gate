"""The HTTP client, tested for the two things that are security behaviour
rather than plumbing: every redirect hop is re-validated, and credentials do
not cross hosts. Connection reuse is proved by counting sockets, not asserted.

parity-gate:allow-secrets-file - the bearer values below are fixtures used to
prove they are stripped, never live credentials.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from parity_gate.httpclient import MAX_REDIRECTS, Client
from parity_gate.safety import Policy, SafetyError


class _Server(ThreadingHTTPServer):
    """A programmable server that records what it received."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, Any] = {}
        self.lock = threading.Lock()

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        with self.lock:
            self.connections += 1
        super().process_request(request, client_address)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _handle(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        with server.lock:
            server.requests.append(
                {
                    "path": self.path,
                    "method": self.command,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": payload.decode() or None,
                }
            )

        route = server.routes.get(self.path.split("?")[0], {"status": 200, "body": {"ok": True}})
        status = route.get("status", 200)
        headers = route.get("headers", {})
        body = route.get("body")
        raw = b"" if body is None else json.dumps(body).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = _handle


@pytest.fixture
def server() -> Iterator[_Server]:
    instance = _Server()
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


def client(**overrides: Any) -> Client:
    policy = Policy(allowed_hosts=["127.0.0.1"], allow_private_networks=True, **overrides)
    return Client(policy)


# -- what the rewrite was for ----------------------------------------------


def test_connections_are_reused_across_requests(server: _Server) -> None:
    """The point of the pool. Twelve requests used to mean twelve handshakes."""
    connection = client()
    try:
        for _ in range(12):
            assert connection.request("GET", f"{server.url}/thing").status == 200
    finally:
        connection.close()

    assert len(server.requests) == 12
    assert server.connections == 1, f"expected one socket, server accepted {server.connections}"


def test_a_dead_pooled_connection_is_recovered_for_an_idempotent_method(
    server: _Server,
) -> None:
    connection = client()
    try:
        connection.request("GET", f"{server.url}/thing")
        # Simulate the peer having closed it while we were not looking.
        for pooled in connection.pool._connections.values():
            pooled.sock.close()  # type: ignore[union-attr]
        assert connection.request("GET", f"{server.url}/thing").status == 200
    finally:
        connection.close()


def test_closing_the_client_releases_every_connection(server: _Server) -> None:
    connection = client()
    connection.request("GET", f"{server.url}/thing")
    assert connection.pool.size == 1
    connection.close()
    assert connection.pool.size == 0


# -- redirects: the security half -------------------------------------------


def test_a_redirect_is_followed_and_recorded(server: _Server) -> None:
    server.routes["/start"] = {"status": 302, "headers": {"Location": "/end"}}
    server.routes["/end"] = {"status": 200, "body": {"arrived": True}}

    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/start")
    finally:
        connection.close()

    assert response.status == 200
    assert response.json_body == {"arrived": True}
    assert response.redirects == [f"{server.url}/end"]


def test_a_redirect_off_the_allow_list_is_refused_before_the_next_request(
    server: _Server,
) -> None:
    """The staging host that 302s to production. The check has to happen before
    the packet, which is why this raises instead of returning a response."""
    server.routes["/start"] = {
        "status": 302,
        "headers": {"Location": "https://api.prod.example.com/x"},
    }
    connection = client()
    try:
        with pytest.raises(SafetyError, match="not in allowed_hosts"):
            connection.request("GET", f"{server.url}/start")
    finally:
        connection.close()
    # One request made, none to the redirect target.
    assert [r["path"] for r in server.requests] == ["/start"]


def test_credentials_are_not_replayed_across_a_host_change(server: _Server) -> None:
    """A token issued for one host has no business reaching another, even when
    that host is also on the allow-list."""
    other = _Server()
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        server.routes["/start"] = {"status": 302, "headers": {"Location": f"{other.url}/end"}}
        other.routes["/end"] = {"status": 200, "body": {"ok": True}}

        connection = client()
        try:
            connection.request(
                "GET",
                f"{server.url}/start",
                headers={"Authorization": "Bearer s3cret0000", "X-Trace": "keep-me"},
            )
        finally:
            connection.close()

        forwarded = other.requests[0]["headers"]
        assert "authorization" not in forwarded
        assert forwarded["x-trace"] == "keep-me"  # ordinary headers survive
    finally:
        other.shutdown()
        other.server_close()


def test_credentials_survive_a_redirect_within_the_same_host(server: _Server) -> None:
    server.routes["/start"] = {"status": 302, "headers": {"Location": "/end"}}
    connection = client()
    try:
        connection.request(
            "GET", f"{server.url}/start", headers={"Authorization": "Bearer s3cret0000"}
        )
    finally:
        connection.close()
    assert server.requests[1]["headers"]["authorization"] == "Bearer s3cret0000"


def test_303_degrades_a_write_to_a_get(server: _Server) -> None:
    server.routes["/submit"] = {"status": 303, "headers": {"Location": "/result"}}
    connection = client(allow_mutations=True)
    try:
        connection.request("POST", f"{server.url}/submit", body={"a": 1})
    finally:
        connection.close()

    assert server.requests[0]["method"] == "POST"
    assert server.requests[1]["method"] == "GET"
    assert server.requests[1]["body"] is None


def test_307_preserves_the_method_and_the_body(server: _Server) -> None:
    server.routes["/submit"] = {"status": 307, "headers": {"Location": "/elsewhere"}}
    connection = client(allow_mutations=True)
    try:
        connection.request("POST", f"{server.url}/submit", body={"a": 1})
    finally:
        connection.close()

    assert [r["method"] for r in server.requests] == ["POST", "POST"]
    assert json.loads(server.requests[1]["body"]) == {"a": 1}


def test_a_redirect_loop_stops_instead_of_spinning(server: _Server) -> None:
    server.routes["/loop"] = {"status": 302, "headers": {"Location": "/loop"}}
    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/loop")
    finally:
        connection.close()

    assert "stopped after" in (response.json_error or "")
    assert len(server.requests) == MAX_REDIRECTS + 1


# -- ordinary behaviour ------------------------------------------------------


def test_an_error_status_is_a_result_not_an_exception(server: _Server) -> None:
    server.routes["/gone"] = {"status": 410, "body": {"error": "gone"}}
    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/gone")
    finally:
        connection.close()

    assert response.status == 410
    assert response.json_body == {"error": "gone"}
    assert response.transport_error is None


def test_retries_apply_only_to_the_statuses_that_deserve_them(server: _Server) -> None:
    server.routes["/flaky"] = {"status": 503, "body": {"busy": True}}
    connection = client(max_retries=2)
    try:
        response = connection.request("GET", f"{server.url}/flaky")
    finally:
        connection.close()

    assert response.status == 503
    assert response.attempts == 3
    assert len(server.requests) == 3


def test_retries_are_off_unless_asked_for(server: _Server) -> None:
    server.routes["/flaky"] = {"status": 503, "body": {"busy": True}}
    connection = client()
    try:
        assert connection.request("GET", f"{server.url}/flaky").attempts == 1
    finally:
        connection.close()
    assert len(server.requests) == 1


def test_an_unreachable_host_becomes_a_transport_error(server: _Server) -> None:
    connection = client()
    try:
        response = connection.request("GET", "http://127.0.0.1:1/nothing")
    finally:
        connection.close()
    assert response.status is None
    assert response.transport_error


def test_a_url_the_policy_forbids_never_leaves(server: _Server) -> None:
    connection = client()
    try:
        with pytest.raises(SafetyError):
            connection.request("GET", "https://api.example.com/x")
    finally:
        connection.close()
    assert server.requests == []


def test_a_query_string_reaches_the_server_intact(server: _Server) -> None:
    connection = client()
    try:
        connection.request("GET", f"{server.url}/search?q=phone&limit=5")
    finally:
        connection.close()
    assert server.requests[0]["path"] == "/search?q=phone&limit=5"
